"""
bot/bot.py — FastAPI app: webhook entry point, startup/shutdown, health check.
"""

import asyncio
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import redis.asyncio as aioredis

from shared.config import REDIS_URL, TASK_QUEUE
from bot.handlers import handle_callback, handle_message
from bot.watchdog import worker_watchdog, task_notifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")

app   = FastAPI(title="TG Claude Bot")
redis: aioredis.Redis | None = None


@app.on_event("startup")
async def startup():
    global redis
    redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    log.info("Redis connected")
    asyncio.create_task(worker_watchdog(redis))
    asyncio.create_task(task_notifier(redis))


@app.on_event("shutdown")
async def shutdown():
    if redis:
        await redis.aclose()


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()

    if body.get("callback_query"):
        return await handle_callback(redis, body)

    message = body.get("message") or body.get("edited_message")
    if message:
        return await handle_message(redis, message)

    return JSONResponse({"ok": True})


@app.get("/health")
async def health():
    q = await redis.llen(TASK_QUEUE)
    return {"status": "ok", "queue": q}
