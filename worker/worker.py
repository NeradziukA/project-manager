"""
worker/worker.py:
  1. Takes a task from Redis
  2. git pull (updates repo)
  3. Runs Claude Code CLI with prompt
  4. git add + commit + push origin + build + pm2 restart
  5. Sends result to Telegram

Task result statuses: "ok" | "fail" | "question" | "rate_limit"
"""

import json
import logging
import asyncio
from datetime import datetime

import httpx
import redis.asyncio as aioredis

from shared.config import (
    REDIS_URL, TASK_QUEUE, RESULT_KEY, FAILED_QUEUE,
    WAITING_PREFIX, PROGRESS_KEY,
    HEARTBEAT_KEY, HEARTBEAT_TTL, HEARTBEAT_INTERVAL,
    QUESTION_MARKER,
)
from worker.telegram import tg_send, tg_edit, chunks
from worker.git_utils import git, get_diff, vds_deploy
from worker.claude_runner import run_claude, is_rate_limited

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("worker")


async def process_task(redis_client: aioredis.Redis, task_raw: str) -> str:
    """
    Process a task. Returns:
      "ok"         — completed successfully
      "fail"       — failed, should be re-queued
      "question"   — Claude asked a question, task stored in waiting state
      "rate_limit" — Claude rate limit hit, task stored in waiting state
    """
    task       = json.loads(task_raw)
    task_id    = task["task_id"]
    task_num   = task.get("task_num")
    prompt     = task["prompt"]
    chat_id    = task["chat_id"]
    message_id = task.get("message_id")
    ack_id     = task.get("ack_msg_id")
    retry      = task.get("retry", 0)

    log.info("Task %s #%s (attempt #%d): %s", task_id, task_num, retry + 1, prompt[:80])
    started = datetime.utcnow()

    async with httpx.AsyncClient(timeout=30) as client:

        retry_str = f" (попытка #{retry + 1})" if retry > 0 else ""
        num_str   = f" #{task_num}" if task_num else ""

        # ── notify chat if no ack message yet ────────────────────────────────
        if not ack_id:
            r = await tg_send(
                client, chat_id,
                f"⚙️ *Задача{num_str} принята в работу{retry_str}*\n\n`{prompt}`",
                reply_to=message_id,
            )
            ack_id = r.get("result", {}).get("message_id")

        # ── step 1: git pull ──────────────────────────────────────────────────
        if ack_id:
            await tg_edit(client, chat_id, ack_id,
                          f"🔄 *Задача{num_str} — Шаг 1/3{retry_str}*\nОбновляю репозиторий...")
        rc, _, err = git("pull", "--rebase")
        if rc != 0:
            log.warning("git pull failed: %s", err)

        # ── step 2: Claude Code ───────────────────────────────────────────────
        if ack_id:
            await tg_edit(client, chat_id, ack_id,
                          f"🤖 *Задача{num_str} — Шаг 2/3*\nClaude Code выполняет задачу...\n\nЭто может занять несколько минут.")

        claude_ok, claude_out = await run_claude(prompt)
        elapsed = round((datetime.utcnow() - started).total_seconds(), 1)

        # ── detect rate limit ─────────────────────────────────────────────────
        if not claude_ok and is_rate_limited(claude_out):
            log.warning("Task %s hit Claude rate limit — parking in waiting state", task_id)
            if task_num:
                await redis_client.set(
                    f"{WAITING_PREFIX}{task_num}",
                    json.dumps(task, ensure_ascii=False),
                )
            rl_msg = (
                f"⏸ *Задача{num_str} — лимит Claude Code*\n\n"
                f"`{claude_out}`\n\n"
                f"Когда лимит сбросится, ответьте: `/answer_{task_num} продолжить`"
            )
            if ack_id:
                await tg_edit(client, chat_id, ack_id, rl_msg)
            else:
                await tg_send(client, chat_id, rl_msg, reply_to=message_id)
            return "rate_limit"

        # ── detect question from Claude (marker at start OR end of output) ──────
        out_stripped = claude_out.strip()
        last_line = out_stripped.splitlines()[-1].strip() if out_stripped else ""
        has_question_marker = (
            out_stripped.upper().startswith(QUESTION_MARKER.upper()) or
            last_line.upper().startswith(QUESTION_MARKER.upper())
        )
        if claude_ok and has_question_marker:
            # extract from whichever line has the marker
            marker_line = last_line if last_line.upper().startswith(QUESTION_MARKER.upper()) else out_stripped
            question = marker_line[len(QUESTION_MARKER):].strip()
            log.info("Task %s has a question: %s", task_id, question[:100])

            if task_num:
                await redis_client.set(
                    f"{WAITING_PREFIX}{task_num}",
                    json.dumps(task, ensure_ascii=False),
                )

            q_msg = (
                f"❓ *Задача{num_str} — вопрос от Claude:*\n\n"
                f"{question}\n\n"
                f"Ответьте: `/answer_{task_num} ваш ответ`"
            )
            if ack_id:
                await tg_edit(client, chat_id, ack_id, q_msg)
            else:
                await tg_send(client, chat_id, q_msg, reply_to=message_id)
            return "question"

        # ── step 3: build + pm2 restart ───────────────────────────────────────
        deploy_status = "не выполнялся"
        if claude_ok:
            if ack_id:
                await tg_edit(client, chat_id, ack_id,
                              f"🚀 *Задача{num_str} — Шаг 3/3*\nСборка и перезапуск сервера...")
            push_ok, push_msg = await vds_deploy()
            deploy_status = push_msg
        else:
            deploy_status = "⏭ Пропущен (Claude Code завершился с ошибкой)"

        # ── diff ──────────────────────────────────────────────────────────────
        diff = ""
        try:
            diff_raw = get_diff()
            if diff_raw and diff_raw != "нет изменений":
                diff = f"\n\n📂 *Изменения:*\n```\n{diff_raw[:600]}\n```"
        except Exception:
            pass

        # ── result message ────────────────────────────────────────────────────
        icon = "✅" if claude_ok else "❌"
        attempt_label = f" • попытка #{retry + 1}" if retry > 0 else ""
        if not claude_ok:
            error_preview = claude_out.strip()[:300]
            fail_reason = f"\n\n⚠️ *Причина:* `{error_preview}`"
        else:
            fail_reason = ""
        header = (
            f"{icon} *{'Задача выполнена' if claude_ok else 'Задача не выполнена — вернул в очередь'}"
            f"{num_str}* (⏱ {elapsed}с{attempt_label})"
            f"{fail_reason}\n\n"
            f"🚀 *Деплой:* {deploy_status}"
            f"{diff}\n\n"
            f"📋 *Вывод Claude Code:*\n"
        )

        parts = chunks(claude_out)
        first = header + parts[0]

        if ack_id:
            await tg_edit(client, chat_id, ack_id, first)
        else:
            await tg_send(client, chat_id, first, reply_to=message_id)

        for part in parts[1:]:
            await tg_send(client, chat_id, part)

    # ── save for /status ──────────────────────────────────────────────────────
    await redis_client.set(RESULT_KEY, json.dumps({
        "task_id"       : task_id,
        "task_num"      : task_num,
        "prompt"        : prompt,
        "success"       : claude_ok,
        "elapsed"       : elapsed,
        "deploy_status" : deploy_status,
        "finished"      : datetime.utcnow().isoformat(),
        "retry"         : retry,
    }, ensure_ascii=False))

    return "ok" if claude_ok else "fail"


async def heartbeat_writer(redis_client: aioredis.Redis) -> None:
    while True:
        try:
            await redis_client.set(HEARTBEAT_KEY, datetime.utcnow().isoformat(), ex=HEARTBEAT_TTL)
        except Exception as e:
            log.warning("Heartbeat write failed: %s", e)
        await asyncio.sleep(HEARTBEAT_INTERVAL)


async def recover_stale_task(redis_client: aioredis.Redis) -> None:
    """On startup, requeue any task that was interrupted mid-processing."""
    raw = await redis_client.get(PROGRESS_KEY)
    if not raw:
        return
    try:
        task = json.loads(raw)
        task["retry"] = task.get("retry", 0) + 1
        await redis_client.rpush(TASK_QUEUE, json.dumps(task, ensure_ascii=False))
        await redis_client.delete(PROGRESS_KEY)
        log.info("Recovered stale task #%s → requeued as retry #%d",
                 task.get("task_num"), task["retry"])
        # Notify about the crash recovery
        chat_id = task.get("chat_id")
        task_num = task.get("task_num")
        ack_id = task.get("ack_msg_id")
        if chat_id:
            num_str = f" #{task_num}" if task_num else ""
            msg = (
                f"⚠️ *Задача{num_str} — воркер был перезапущен*\n\n"
                f"Задача была прервана и поставлена в очередь повторно "
                f"(попытка #{task['retry'] + 1})."
            )
            async with httpx.AsyncClient(timeout=15) as client:
                if ack_id:
                    await tg_edit(client, chat_id, ack_id, msg)
                else:
                    await tg_send(client, chat_id, msg)
    except Exception as e:
        log.warning("Failed to recover stale task: %s", e)


async def main():
    log.info("Worker started.")
    redis_client = await aioredis.from_url(REDIS_URL, decode_responses=True)
    await recover_stale_task(redis_client)
    asyncio.create_task(heartbeat_writer(redis_client))
    try:
        while True:
            item = await redis_client.blpop(TASK_QUEUE, timeout=2)
            if item is None:
                continue
            _, raw = item

            await redis_client.set(PROGRESS_KEY, raw)

            result = "fail"
            try:
                result = await process_task(redis_client, raw)
            except Exception as e:
                log.exception("Task failed with exception: %s", e)
                await redis_client.rpush(FAILED_QUEUE, raw)
                try:
                    task = json.loads(raw)
                    chat_id = task.get("chat_id")
                    task_num = task.get("task_num")
                    ack_id = task.get("ack_msg_id")
                    if chat_id:
                        num_str = f" #{task_num}" if task_num else ""
                        msg = (
                            f"💥 *Задача{num_str} — необработанная ошибка*\n\n"
                            f"`{type(e).__name__}: {str(e)[:300]}`\n\n"
                            f"Задача перемещена в очередь ошибок."
                        )
                        async with httpx.AsyncClient(timeout=15) as client:
                            if ack_id:
                                await tg_edit(client, chat_id, ack_id, msg)
                            else:
                                await tg_send(client, chat_id, msg)
                except Exception:
                    pass
            finally:
                await redis_client.delete(PROGRESS_KEY)

            if result == "fail":
                task = json.loads(raw)
                task["retry"] = task.get("retry", 0) + 1
                if task["retry"] >= 3:
                    log.warning("Task %s exceeded max retries — moving to failed queue", task["task_id"])
                    await redis_client.rpush(FAILED_QUEUE, json.dumps(task, ensure_ascii=False))
                    chat_id = task.get("chat_id")
                    task_num = task.get("task_num")
                    ack_id = task.get("ack_msg_id")
                    if chat_id:
                        num_str = f" #{task_num}" if task_num else ""
                        msg = f"🚫 *Задача{num_str} — превышен лимит попыток*\n\nЗадача выполнялась 3 раза и каждый раз завершалась ошибкой. Перемещена в очередь ошибок."
                        async with httpx.AsyncClient(timeout=15) as client:
                            if ack_id:
                                await tg_edit(client, chat_id, ack_id, msg)
                            else:
                                await tg_send(client, chat_id, msg)
                else:
                    await redis_client.rpush(TASK_QUEUE, json.dumps(task, ensure_ascii=False))
                    log.info("Re-queued task %s as retry #%d", task["task_id"], task["retry"])
            # "ok", "question", "rate_limit" — не перезапускаем
    finally:
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
