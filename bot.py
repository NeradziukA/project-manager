"""
bot.py — FastAPI webhook, accepts tasks from Telegram and puts them into Redis.

Task lifecycle:
  pending  → user sent task, awaiting /ok_N confirmation
  tasks    → confirmed, worker picks it up
  progress → worker is currently executing
  waiting  → Claude asked a question, waiting for /answer_N
"""

import os
import re
import json
import logging
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx
import redis.asyncio as aioredis
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")

BOT_TOKEN      = os.environ["TELEGRAM_BOT_TOKEN"]
REDIS_URL      = os.getenv("REDIS_URL",       "redis://localhost:6379")
ALLOWED_IDS    = set(int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip())
TASK_QUEUE     = os.getenv("TASK_QUEUE",      "claude:tasks")
RESULT_KEY     = os.getenv("RESULT_KEY",      "claude:last_result")
TASK_COUNTER   = os.getenv("TASK_COUNTER",    "claude:task_counter")
PENDING_PREFIX = os.getenv("PENDING_PREFIX",  "claude:pending:")
WAITING_PREFIX = os.getenv("WAITING_PREFIX",  "claude:waiting:")
PROGRESS_KEY   = os.getenv("PROGRESS_KEY",    "claude:in_progress")
API_BASE       = f"https://api.telegram.org/bot{BOT_TOKEN}"

app   = FastAPI(title="TG Claude Bot")
redis: aioredis.Redis | None = None


@app.on_event("startup")
async def startup():
    global redis
    redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    log.info("Redis connected")


@app.on_event("shutdown")
async def shutdown():
    if redis:
        await redis.aclose()


async def tg(method: str, **kwargs) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{API_BASE}/{method}", json=kwargs)
        return r.json()


async def send(chat_id: int, text: str, reply_to: int | None = None) -> dict:
    kwargs = {"chat_id": chat_id, "text": text[:4096], "parse_mode": "Markdown"}
    if reply_to:
        kwargs["reply_to_message_id"] = reply_to
    return await tg("sendMessage", **kwargs)


async def edit(chat_id: int, msg_id: int, text: str) -> dict:
    return await tg("editMessageText",
                    chat_id=chat_id, message_id=msg_id,
                    text=text[:4096], parse_mode="Markdown")


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    message = body.get("message") or body.get("edited_message")
    if not message:
        return JSONResponse({"ok": True})

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

    # ── /start ────────────────────────────────────────────────────────────────
    if text == "/start":
        await send(chat_id, (
            "👋 *Claude Code + Heroku Bot*\n\n"
            "Напиши задачу — менеджер попросит подтверждение, затем Claude Code изменит код и задеплоит.\n\n"
            "*/queue* — очередь задач\n"
            "*/status* — статус последней задачи\n\n"
            "После получения задачи:\n"
            "  `/ok_N` — подтвердить задачу N\n"
            "  `/cancel_N` — отменить задачу N\n"
            "  `/answer_N текст` — ответить на вопрос по задаче N"
        ))
        return JSONResponse({"ok": True})

    # ── /queue ─────────────────────────────────────────────────────────────────
    if text == "/queue":
        lines = []

        # In progress
        progress_raw = await redis.get(PROGRESS_KEY)
        if progress_raw:
            p = json.loads(progress_raw)
            lines.append(f"⚙️ *В работе:* #{p.get('task_num', '?')} `{p['prompt'][:80]}`")

        # Active queue
        queue_items = await redis.lrange(TASK_QUEUE, 0, -1)
        if queue_items:
            lines.append(f"\n📋 *В очереди ({len(queue_items)}):*")
            for raw in queue_items:
                t = json.loads(raw)
                lines.append(f"  #{t.get('task_num', '?')} `{t['prompt'][:80]}`")

        # Pending confirmation
        pending_keys = [k async for k in redis.scan_iter(f"{PENDING_PREFIX}*")]
        if pending_keys:
            lines.append(f"\n⏳ *Ожидают подтверждения ({len(pending_keys)}):*")
            for k in sorted(pending_keys):
                raw = await redis.get(k)
                if raw:
                    t = json.loads(raw)
                    num = k.replace(PENDING_PREFIX, "")
                    lines.append(f"  #{num} `{t['prompt'][:80]}`")

        # Waiting for answer
        waiting_keys = [k async for k in redis.scan_iter(f"{WAITING_PREFIX}*")]
        if waiting_keys:
            lines.append(f"\n❓ *Ждут ответа ({len(waiting_keys)}):*")
            for k in sorted(waiting_keys):
                raw = await redis.get(k)
                if raw:
                    t = json.loads(raw)
                    num = k.replace(WAITING_PREFIX, "")
                    lines.append(f"  #{num} `{t['prompt'][:80]}`")

        if not lines:
            await send(chat_id, "📋 Очередь пуста")
        else:
            await send(chat_id, "\n".join(lines))
        return JSONResponse({"ok": True})

    # ── /status ────────────────────────────────────────────────────────────────
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

    # ── /ok_N — confirm pending task ──────────────────────────────────────────
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
                await edit(chat_id, ack, f"✅ *Задача #{num} подтверждена*\nОжидает выполнения...")
            await send(chat_id, f"✅ Задача *#{num}* отправлена в работу.", reply_to=message_id)
            log.info("Task #%s confirmed and queued", num)
        else:
            await send(chat_id, f"❓ Задача *#{num}* не найдена среди ожидающих.")
        return JSONResponse({"ok": True})

    # ── /cancel_N — cancel pending task ───────────────────────────────────────
    m = re.match(r"^/cancel[_ ](\d+)$", text, re.IGNORECASE)
    if m:
        num = m.group(1)
        raw = await redis.get(f"{PENDING_PREFIX}{num}")
        if raw:
            await redis.delete(f"{PENDING_PREFIX}{num}")
            task = json.loads(raw)
            ack = task.get("ack_msg_id")
            if ack:
                await edit(chat_id, ack, f"🚫 *Задача #{num} отменена*")
            await send(chat_id, f"🚫 Задача *#{num}* отменена.")
            log.info("Task #%s cancelled", num)
        else:
            await send(chat_id, f"❓ Задача *#{num}* не найдена среди ожидающих.")
        return JSONResponse({"ok": True})

    # ── /answer_N text — reply to Claude's question ───────────────────────────
    m = re.match(r"^/answer[_ ](\d+)\s+(.+)$", text, re.IGNORECASE | re.DOTALL)
    if m:
        num = m.group(1)
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

    # ── new task → pending, awaiting confirmation ─────────────────────────────
    task_num = await redis.incr(TASK_COUNTER)
    ack = await send(
        chat_id,
        f"📋 *Задача #{task_num}*\n\n`{text[:300]}`\n\n"
        f"Подтвердить: `/ok_{task_num}`\n"
        f"Отменить: `/cancel_{task_num}`",
        reply_to=message_id,
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


@app.get("/health")
async def health():
    q = await redis.llen(TASK_QUEUE)
    return {"status": "ok", "queue": q}
