"""
worker.py:
  1. Takes a task from Redis
  2. git pull (updates repo)
  3. Runs Claude Code CLI with prompt
  4. git add + commit + push heroku
  5. Sends result to Telegram

Task result statuses: "ok" | "fail" | "question"
"""

import os
import json
import logging
import asyncio
import subprocess
import shutil
from datetime import datetime
from pathlib import Path

import httpx
import redis.asyncio as aioredis
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("worker")

# ── config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.environ["TELEGRAM_BOT_TOKEN"]
REDIS_URL       = os.getenv("REDIS_URL",       "redis://localhost:6379")
TASK_QUEUE      = os.getenv("TASK_QUEUE",      "claude:tasks")
RESULT_KEY      = os.getenv("RESULT_KEY",      "claude:last_result")
FAILED_QUEUE    = os.getenv("FAILED_QUEUE",    "claude:failed")
WAITING_PREFIX  = os.getenv("WAITING_PREFIX",  "claude:waiting:")
PROGRESS_KEY    = os.getenv("PROGRESS_KEY",    "claude:in_progress")

REPO_DIR        = Path(os.environ["REPO_DIR"])
GIT_REMOTE      = os.getenv("HEROKU_GIT_REMOTE", "heroku")

CLAUDE_TIMEOUT  = int(os.getenv("CLAUDE_TIMEOUT", "300"))
HEARTBEAT_KEY   = os.getenv("HEARTBEAT_KEY",   "claude:worker:heartbeat")
HEARTBEAT_TTL   = 30    # seconds until key expires — if missing, worker is down
HEARTBEAT_INTERVAL = 10  # how often to refresh
API_BASE        = f"https://api.telegram.org/bot{BOT_TOKEN}"

MAX_MSG         = 3800   # chars per Telegram message

# Marker Claude must use when it needs clarification
QUESTION_MARKER = "QUESTION:"

# Instruction prepended to every prompt
QUESTION_INSTRUCTION = (
    "IMPORTANT SYSTEM RULE: You have full permissions to read and write all files — "
    "never ask for file write permissions. "
    "If you need clarification from the user before proceeding, output exactly: "
    f"{QUESTION_MARKER} [your question] — and nothing else. "
    "Otherwise, proceed with the task immediately.\n\n"
)


# ── Telegram ───────────────────────────────────────────────────────────────────
async def tg_send(client: httpx.AsyncClient, chat_id: int, text: str,
                  reply_to: int | None = None) -> dict:
    p = {"chat_id": chat_id, "text": text[:4096], "parse_mode": "Markdown"}
    if reply_to:
        p["reply_to_message_id"] = reply_to
    r = await client.post(f"{API_BASE}/sendMessage", json=p)
    return r.json()


async def tg_edit(client: httpx.AsyncClient, chat_id: int,
                  msg_id: int, text: str) -> dict:
    r = await client.post(f"{API_BASE}/editMessageText", json={
        "chat_id": chat_id, "message_id": msg_id,
        "text": text[:4096], "parse_mode": "Markdown",
    })
    return r.json()


def chunks(text: str, size: int = MAX_MSG) -> list[str]:
    parts = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size) or size
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts or ["(нет вывода)"]


# ── git helpers ────────────────────────────────────────────────────────────────
def git(*args, cwd: Path = REPO_DIR) -> tuple[int, str, str]:
    """Run a git command, return (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["git", *args], cwd=str(cwd),
        capture_output=True, text=True
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def get_diff() -> str:
    """Get git diff stat of the last commit (what Claude Code changed)."""
    _, diff, _ = git("diff", "HEAD~1", "HEAD", "--stat")
    return diff or "нет изменений"


def get_commit_hash() -> str:
    _, h, _ = git("rev-parse", "--short", "HEAD")
    return h


# ── Claude Code ────────────────────────────────────────────────────────────────
def find_claude() -> str:
    path = shutil.which("claude")
    if path:
        return path
    for c in ["/usr/local/bin/claude",
               str(Path.home() / ".npm-global/bin/claude"),
               str(Path.home() / ".local/bin/claude")]:
        if Path(c).exists():
            return c
    raise FileNotFoundError("Claude Code CLI not found. Install: npm install -g @anthropic-ai/claude-code")


async def run_claude(prompt: str) -> tuple[bool, str]:
    """Run claude --print in the repo directory."""
    claude_bin = find_claude()
    full_prompt = QUESTION_INSTRUCTION + prompt
    try:
        proc = await asyncio.create_subprocess_exec(
            claude_bin, "--print", "--dangerously-skip-permissions", "--output-format", "text", full_prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(REPO_DIR),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CLAUDE_TIMEOUT)
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode == 0:
            return True, out or "(пустой вывод)"
        return False, out or err or f"Код выхода: {proc.returncode}"

    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return False, f"⏰ Таймаут {CLAUDE_TIMEOUT}с — задача слишком долгая"
    except Exception as e:
        return False, f"❌ Ошибка запуска Claude Code: {e}"


# ── Heroku deploy ──────────────────────────────────────────────────────────────
async def heroku_deploy() -> tuple[bool, str]:
    """git push heroku main, return (success, status message)."""
    # 1. commit if there are changes
    rc, status_out, _ = git("status", "--porcelain")
    if status_out:
        git("add", "-A")
        rc, _, err = git("commit", "-m", f"feat: claude code task [{datetime.utcnow().strftime('%H:%M')}]")
        if rc != 0:
            return False, f"git commit failed: {err}"
    else:
        return True, "Нет изменений файлов — деплой не нужен"

    # 2. push to heroku
    branch = "main"
    rc, out, err = git("push", GIT_REMOTE, branch)
    if rc != 0:
        # try master
        rc, out, err = git("push", GIT_REMOTE, "master")
    if rc != 0:
        return False, f"git push failed:\n{err}"

    return True, "Push успешен, Heroku собирает..."


# ── main loop ──────────────────────────────────────────────────────────────────
async def process_task(redis_client: aioredis.Redis, task_raw: str) -> str:
    """
    Process a task. Returns:
      "ok"       — completed successfully
      "fail"     — failed, should be re-queued
      "question" — Claude asked a question, task stored in waiting state
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

        # ── detect question from Claude ───────────────────────────────────────
        if claude_ok and claude_out.strip().upper().startswith(QUESTION_MARKER.upper()):
            question = claude_out.strip()[len(QUESTION_MARKER):].strip()
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

        # ── step 3: deploy to Heroku ──────────────────────────────────────────
        deploy_status = "не выполнялся"
        if claude_ok:
            if ack_id:
                await tg_edit(client, chat_id, ack_id,
                              f"🚀 *Задача{num_str} — Шаг 3/3*\nДеплой на Heroku...")
            push_ok, push_msg = await heroku_deploy()
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
        header = (
            f"{icon} *{'Задача выполнена' if claude_ok else 'Задача не выполнена — вернул в очередь'}"
            f"{num_str}* (⏱ {elapsed}с{attempt_label})\n\n"
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
    """Writes a heartbeat key to Redis every HEARTBEAT_INTERVAL seconds.
    If the key disappears (TTL expired), the manager bot knows the worker is down.
    """
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
    except Exception as e:
        log.warning("Failed to recover stale task: %s", e)


async def main():
    log.info("Worker started. Repo: %s", REPO_DIR)
    redis_client = await aioredis.from_url(REDIS_URL, decode_responses=True)
    await recover_stale_task(redis_client)
    asyncio.create_task(heartbeat_writer(redis_client))
    try:
        while True:
            item = await redis_client.blpop(TASK_QUEUE, timeout=2)
            if item is None:
                continue
            _, raw = item

            # Track in-progress task so /queue can show it
            await redis_client.set(PROGRESS_KEY, raw)

            result = "fail"
            try:
                result = await process_task(redis_client, raw)
            except Exception as e:
                log.exception("Task failed with exception: %s", e)
                await redis_client.rpush(FAILED_QUEUE, raw)
            finally:
                await redis_client.delete(PROGRESS_KEY)

            if result == "fail":
                task = json.loads(raw)
                task["retry"] = task.get("retry", 0) + 1
                await redis_client.rpush(TASK_QUEUE, json.dumps(task, ensure_ascii=False))
                log.info("Re-queued task %s as retry #%d", task["task_id"], task["retry"])
    finally:
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
