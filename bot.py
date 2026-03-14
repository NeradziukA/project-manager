"""
bot.py — FastAPI webhook, принимает задачи из Telegram и кладёт в Redis.
"""

import os
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

BOT_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
REDIS_URL   = os.getenv("REDIS_URL", "redis://localhost:6379")
ALLOWED_IDS = set(int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip())
TASK_QUEUE  = os.getenv("TASK_QUEUE", "claude:tasks")
RESULT_KEY  = os.getenv("RESULT_KEY", "claude:last_result")
API_BASE    = f"https://api.telegram.org/bot{BOT_TOKEN}"

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

    if text == "/start":
        await send(chat_id, (
            "👋 *Claude Code + Heroku Bot*\n\n"
            "Напиши задачу — Claude Code изменит код в репозитории и задеплоит на Heroku.\n\n"
            "*/queue* — очередь задач\n"
            "*/status* — статус последнего деплоя"
        ))
        return JSONResponse({"ok": True})

    if text == "/queue":
        length = await redis.llen(TASK_QUEUE)
        await send(chat_id, f"📋 Задач в очереди: *{length}*")
        return JSONResponse({"ok": True})

    if text == "/status":
        last = await redis.get(RESULT_KEY)
        if last:
            data = json.loads(last)
            status = "✅" if data.get("success") else "❌"
            await send(chat_id, (
                f"{status} *Последняя задача*\n\n"
                f"📝 `{data['prompt'][:100]}`\n"
                f"⏱ {data.get('elapsed', '?')}с\n"
                f"🚀 Деплой: {data.get('deploy_status', 'неизвестно')}"
            ))
        else:
            await send(chat_id, "Задач ещё не было.")
        return JSONResponse({"ok": True})

    # ── поставить задачу в очередь ────────────────────────────────────────────
    ack = await send(chat_id, "⏳ *Задача принята*\nКлонирую репо и запускаю Claude Code...", reply_to=message_id)
    ack_msg_id = ack.get("result", {}).get("message_id")

    task = {
        "task_id"    : f"{chat_id}:{message_id}:{int(datetime.utcnow().timestamp())}",
        "prompt"     : text,
        "chat_id"    : chat_id,
        "message_id" : message_id,
        "ack_msg_id" : ack_msg_id,
        "created_at" : datetime.utcnow().isoformat(),
    }
    await redis.rpush(TASK_QUEUE, json.dumps(task, ensure_ascii=False))
    log.info("Queued task %s", task["task_id"])
    return JSONResponse({"ok": True})


@app.get("/health")
async def health():
    q = await redis.llen(TASK_QUEUE)
    return {"status": "ok", "queue": q}
