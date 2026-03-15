"""
handlers.py — Telegram message & callback_query processing.
"""

import json
import logging
import re
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from shared.config import (
    ALLOWED_IDS, TASK_QUEUE, RESULT_KEY, TASK_COUNTER,
    PENDING_PREFIX, WAITING_PREFIX, PROGRESS_KEY, PM2_APP_NAME,
)
from bot.telegram import tg, send, edit, confirm_keyboard

log = logging.getLogger("bot.handlers")

PROJECT_DIR = Path(__file__).resolve().parent.parent


# ── callback_query (inline button press) ─────────────────────────────────────
async def handle_callback(redis, body: dict) -> JSONResponse:
    cb = body["callback_query"]
    cb_id      = cb["id"]
    cb_data    = cb.get("data", "")
    chat_id    = cb["message"]["chat"]["id"]
    msg_id     = cb["message"]["message_id"]
    user_id    = cb["from"]["id"]

    if ALLOWED_IDS and user_id not in ALLOWED_IDS:
        await tg("answerCallbackQuery", callback_query_id=cb_id, text="⛔ Нет доступа")
        return JSONResponse({"ok": True})

    m = re.match(r"^(ok|cancel)_(\d+)$", cb_data)
    if not m:
        await tg("answerCallbackQuery", callback_query_id=cb_id)
        return JSONResponse({"ok": True})

    action, num = m.group(1), m.group(2)
    raw = await redis.get(f"{PENDING_PREFIX}{num}")

    if not raw:
        await tg("answerCallbackQuery", callback_query_id=cb_id,
                 text=f"Задача #{num} не найдена", show_alert=True)
        return JSONResponse({"ok": True})

    if action == "ok":
        await redis.delete(f"{PENDING_PREFIX}{num}")
        await redis.rpush(TASK_QUEUE, raw)
        await edit(chat_id, msg_id,
                   f"✅ *Задача #{num} подтверждена*\nОжидает выполнения...",
                   reply_markup={"inline_keyboard": []})
        await tg("answerCallbackQuery", callback_query_id=cb_id, text="✅ Запущено")
        log.info("Task #%s confirmed via button", num)

    elif action == "cancel":
        await redis.delete(f"{PENDING_PREFIX}{num}")
        await edit(chat_id, msg_id,
                   f"🚫 *Задача #{num} отменена*",
                   reply_markup={"inline_keyboard": []})
        await tg("answerCallbackQuery", callback_query_id=cb_id, text="🚫 Отменено")
        log.info("Task #%s cancelled via button", num)

    return JSONResponse({"ok": True})


# ── message handlers ──────────────────────────────────────────────────────────
async def handle_message(redis, message: dict) -> JSONResponse:
    chat_id    = message["chat"]["id"]
    user_id    = message["from"]["id"]
    text       = (message.get("text") or "").strip()
    message_id = message["message_id"]

    if ALLOWED_IDS and user_id not in ALLOWED_IDS:
        await send(chat_id, "⛔ Нет доступа.")
        return JSONResponse({"ok": True})

    if not text:
        await send(chat_id, "Отправь текстовую задачу для Claude Code.")
        return JSONResponse({"ok": True})

    # /start, /help ───────────────────────────────────────────────────────────
    if text in ("/start", "/help"):
        await send(chat_id, (
            "👋 *Claude Code Bot*\n\n"
            "Напиши задачу — появятся кнопки подтверждения, затем Claude Code изменит код и задеплоит.\n\n"
            "*/queue* — очередь задач\n"
            "*/status* — статус последней задачи\n"
            "*/restart_hives* — перезапустить сервер через pm2\n"
            "*/update_bot* — обновить и перезапустить бот\n\n"
            "После отправки задачи:\n"
            "  `/answer_N текст` — ответить на вопрос по задаче N"
        ))
        return JSONResponse({"ok": True})

    # /queue ───────────────────────────────────────────────────────────────────
    if text == "/queue":
        lines = []

        progress_raw = await redis.get(PROGRESS_KEY)
        if progress_raw:
            p = json.loads(progress_raw)
            lines.append(f"⚙️ *В работе:* #{p.get('task_num', '?')} `{p['prompt'][:300]}`")

        queue_items = await redis.lrange(TASK_QUEUE, 0, -1)
        if queue_items:
            lines.append(f"\n📋 *В очереди ({len(queue_items)}):*")
            for raw in queue_items:
                t = json.loads(raw)
                lines.append(f"  #{t.get('task_num', '?')} `{t['prompt'][:300]}`")

        pending_keys = [k async for k in redis.scan_iter(f"{PENDING_PREFIX}*")]
        if pending_keys:
            lines.append(f"\n⏳ *Ожидают подтверждения ({len(pending_keys)}):*")
            for k in sorted(pending_keys):
                raw = await redis.get(k)
                if raw:
                    t = json.loads(raw)
                    num = k.replace(PENDING_PREFIX, "")
                    lines.append(f"  #{num} `{t['prompt'][:300]}`")

        waiting_keys = [k async for k in redis.scan_iter(f"{WAITING_PREFIX}*")]
        if waiting_keys:
            lines.append(f"\n❓ *Ждут ответа ({len(waiting_keys)}):*")
            for k in sorted(waiting_keys):
                raw = await redis.get(k)
                if raw:
                    t = json.loads(raw)
                    num = k.replace(WAITING_PREFIX, "")
                    lines.append(f"  #{num} `{t['prompt'][:300]}`")

        await send(chat_id, "\n".join(lines) if lines else "📋 Очередь пуста")
        return JSONResponse({"ok": True})

    # /status ──────────────────────────────────────────────────────────────────
    if text == "/status":
        last = await redis.get(RESULT_KEY)
        if last:
            data = json.loads(last)
            status = "✅" if data.get("success") else "❌"
            retry = data.get("retry", 0)
            retry_str = f" (попытка #{retry + 1})" if retry > 0 else ""
            await send(chat_id, (
                f"{status} *Последняя задача{retry_str}*\n\n"
                f"📝 `{data['prompt'][:100]}`\n"
                f"⏱ {data.get('elapsed', '?')}с\n"
                f"🚀 Деплой: {data.get('deploy_status', 'неизвестно')}"
            ))
        else:
            await send(chat_id, "Задач ещё не было.")
        return JSONResponse({"ok": True})

    # /restart_hives ───────────────────────────────────────────────────────────
    if text == "/restart_hives":
        await send(chat_id, f"🔄 Перезапускаю `{PM2_APP_NAME}` через pm2...")
        r = subprocess.run(
            ["npx", "pm2", "restart", PM2_APP_NAME],
            cwd=str(PROJECT_DIR.parent / "hives" / "server"),
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            await send(chat_id, f"✅ `pm2 restart {PM2_APP_NAME}` выполнен")
        else:
            await send(chat_id, f"❌ Ошибка:\n```\n{r.stderr.strip()[:600]}\n```")
        return JSONResponse({"ok": True})

    # /update_bot ──────────────────────────────────────────────────────────────
    if text == "/update_bot":
        pull = subprocess.run(
            ["git", "pull", "--rebase"],
            cwd=str(PROJECT_DIR),
            capture_output=True, text=True,
        )
        if pull.returncode != 0:
            await send(chat_id, f"❌ git pull failed:\n```\n{pull.stderr[:800]}\n```")
            return JSONResponse({"ok": True})

        pull_out = pull.stdout.strip() or "Already up to date."

        # restart worker first — we can get its result
        w = subprocess.run(["sudo", "systemctl", "restart", "hives-worker"],
                           capture_output=True, text=True)
        worker_status = "✅ воркер перезапущен" if w.returncode == 0 else f"❌ воркер: {w.stderr.strip()}"

        await send(chat_id,
                   f"✅ *git pull:*\n```\n{pull_out[:400]}\n```\n"
                   f"{worker_status}\n"
                   f"🔄 Перезапускаю бот... жди `🟢 Бот запущен`")

        log.info("update_bot: restarting hives-bot")
        # small delay so the message is delivered before process dies
        subprocess.Popen(["bash", "-c", "sleep 1 && sudo systemctl restart hives-bot"])
        return JSONResponse({"ok": True})

    # /ok_N (text fallback) ────────────────────────────────────────────────────
    m = re.match(r"^/ok[_ ](\d+)$", text, re.IGNORECASE)
    if m:
        num = m.group(1)
        raw = await redis.get(f"{PENDING_PREFIX}{num}")
        if raw:
            await redis.delete(f"{PENDING_PREFIX}{num}")
            await redis.rpush(TASK_QUEUE, raw)
            task = json.loads(raw)
            ack = task.get("ack_msg_id")
            if ack:
                await edit(chat_id, ack,
                           f"✅ *Задача #{num} подтверждена*\nОжидает выполнения...",
                           reply_markup={"inline_keyboard": []})
            await send(chat_id, f"✅ Задача *#{num}* отправлена в работу.", reply_to=message_id)
            log.info("Task #%s confirmed via command", num)
        else:
            await send(chat_id, f"❓ Задача *#{num}* не найдена среди ожидающих.")
        return JSONResponse({"ok": True})

    # /cancel_N (text fallback) ────────────────────────────────────────────────
    m = re.match(r"^/cancel[_ ](\d+)$", text, re.IGNORECASE)
    if m:
        num = m.group(1)
        raw = await redis.get(f"{PENDING_PREFIX}{num}")
        if raw:
            await redis.delete(f"{PENDING_PREFIX}{num}")
            task = json.loads(raw)
            ack = task.get("ack_msg_id")
            if ack:
                await edit(chat_id, ack,
                           f"🚫 *Задача #{num} отменена*",
                           reply_markup={"inline_keyboard": []})
            await send(chat_id, f"🚫 Задача *#{num}* отменена.")
            log.info("Task #%s cancelled via command", num)
        else:
            await send(chat_id, f"❓ Задача *#{num}* не найдена среди ожидающих.")
        return JSONResponse({"ok": True})

    # /answer_N text ───────────────────────────────────────────────────────────
    m = re.match(r"^/answer[_ ](\d+)\s+(.+)$", text, re.IGNORECASE | re.DOTALL)
    if m:
        num    = m.group(1)
        answer = m.group(2).strip()
        raw = await redis.get(f"{WAITING_PREFIX}{num}")
        if raw:
            task = json.loads(raw)
            task["prompt"] = task["prompt"] + f"\n\nПользователь ответил на вопрос: {answer}"
            await redis.delete(f"{WAITING_PREFIX}{num}")
            await redis.rpush(TASK_QUEUE, json.dumps(task, ensure_ascii=False))
            await send(chat_id, f"✅ Ответ передан, задача *#{num}* возобновлена.", reply_to=message_id)
            log.info("Task #%s resumed with answer", num)
        else:
            await send(chat_id, f"❓ Задача *#{num}* не найдена среди ожидающих ответа.")
        return JSONResponse({"ok": True})

    # new task → pending ───────────────────────────────────────────────────────
    task_num = await redis.incr(TASK_COUNTER)
    ack = await send(
        chat_id,
        f"📋 *Задача #{task_num}*\n\n`{text[:300]}`",
        reply_to=message_id,
        reply_markup=confirm_keyboard(task_num),
    )
    ack_msg_id = ack.get("result", {}).get("message_id")

    task = {
        "task_id"    : f"{chat_id}:{message_id}:{int(datetime.utcnow().timestamp())}",
        "task_num"   : task_num,
        "prompt"     : text,
        "chat_id"    : chat_id,
        "message_id" : message_id,
        "ack_msg_id" : ack_msg_id,
        "created_at" : datetime.utcnow().isoformat(),
    }
    await redis.set(f"{PENDING_PREFIX}{task_num}", json.dumps(task, ensure_ascii=False))
    log.info("Task #%d pending confirmation: %s", task_num, text[:80])
    return JSONResponse({"ok": True})
