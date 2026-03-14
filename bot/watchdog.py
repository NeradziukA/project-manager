import asyncio
import json
import logging

import redis.asyncio as aioredis

from shared.config import (
    TASK_QUEUE, PROGRESS_KEY, HEARTBEAT_KEY,
    PENDING_PREFIX, NOTIFY_QUEUE,
    ALERT_CHAT_ID, NOTIFY_CHAT_ID,
    CHECK_INTERVAL,
)
from bot.telegram import send, confirm_keyboard

log = logging.getLogger("bot.watchdog")


async def worker_watchdog(redis: aioredis.Redis) -> None:
    """Checks worker heartbeat every CHECK_INTERVAL seconds.
    Only alerts if there are tasks waiting — idle worker is fine.
    """
    await asyncio.sleep(60)
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            has_work = (
                await redis.llen(TASK_QUEUE) > 0
                or await redis.exists(PROGRESS_KEY)
            )
            if not has_work:
                continue

            hb = await redis.exists(HEARTBEAT_KEY)
            if not hb and ALERT_CHAT_ID:
                await send(ALERT_CHAT_ID,
                           "⚠️ *Воркер не отвечает!*\n"
                           "Heartbeat устарел, но в очереди есть задачи.\n"
                           "`systemctl restart hives-worker`")
                log.error("Worker heartbeat missing with tasks in queue")
        except Exception as e:
            log.warning("Watchdog check failed: %s", e)


async def task_notifier(redis: aioredis.Redis) -> None:
    """Watches claude:notify for tasks routed by the orchestrator."""
    await asyncio.sleep(2)
    while True:
        try:
            item = await redis.blpop(NOTIFY_QUEUE, timeout=5)
            if not item:
                continue
            _, task_num_str = item
            raw = await redis.get(f"{PENDING_PREFIX}{task_num_str}")
            if not raw:
                log.warning("Notify: pending task #%s not found", task_num_str)
                continue
            task = json.loads(raw)
            notify_chat = NOTIFY_CHAT_ID or task.get("chat_id")
            if not notify_chat:
                log.warning("Notify: no chat_id for task #%s", task_num_str)
                continue

            prompt = task["prompt"]
            ack = await send(
                notify_chat,
                f"📋 *Задача #{task_num_str}*\n\n`{prompt}`",
                reply_markup=confirm_keyboard(int(task_num_str)),
            )
            ack_msg_id = ack.get("result", {}).get("message_id")
            task["ack_msg_id"] = ack_msg_id
            task["chat_id"] = notify_chat
            await redis.set(f"{PENDING_PREFIX}{task_num_str}", json.dumps(task, ensure_ascii=False))
            log.info("Notified chat %s about pending task #%s", notify_chat, task_num_str)
        except Exception as e:
            log.warning("Task notifier error: %s", e)
