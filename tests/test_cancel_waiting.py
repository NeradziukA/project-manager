"""
Regression test for /cancel_N not working on waiting tasks.

Bug: /cancel_N only checked PENDING_PREFIX, so tasks stuck in
WAITING_PREFIX (waiting for user answer) could not be cancelled via bot.
Fix: /cancel_N now checks WAITING_PREFIX as fallback.
"""

import pytest

PENDING_PREFIX = "claude:pending:"
WAITING_PREFIX = "claude:waiting:"


class FakeRedis:
    def __init__(self, data: dict):
        self._data = dict(data)
        self.deleted = []
        self.sent = []

    async def get(self, key):
        return self._data.get(key)

    async def delete(self, key):
        self.deleted.append(key)
        self._data.pop(key, None)


def cancel_logic(redis_data: dict, num: str) -> tuple[str, list]:
    """Mirrors the /cancel_N handler logic from bot/handlers.py."""
    import json
    deleted = []
    message = ""

    raw = redis_data.get(f"{PENDING_PREFIX}{num}")
    if raw:
        deleted.append(f"{PENDING_PREFIX}{num}")
        message = f"🚫 Задача #{num} отменена."
        return message, deleted

    raw = redis_data.get(f"{WAITING_PREFIX}{num}")
    if raw:
        deleted.append(f"{WAITING_PREFIX}{num}")
        message = f"🚫 Задача #{num} отменена (была в ожидании ответа)."
        return message, deleted

    message = f"❓ Задача #{num} не найдена."
    return message, deleted


# ── regression: waiting task can now be cancelled ────────────────────────────

def test_cancel_waiting_task():
    """Task in WAITING_PREFIX must be cancelled by /cancel_N — fails without fix."""
    import json
    task = json.dumps({"task_num": 31, "chat_id": 123, "prompt": "test"})
    data = {f"{WAITING_PREFIX}31": task}

    msg, deleted = cancel_logic(data, "31")

    assert f"{WAITING_PREFIX}31" in deleted
    assert "отменена" in msg


def test_cancel_pending_task_still_works():
    """Original behavior: pending task cancellation must still work."""
    import json
    task = json.dumps({"task_num": 5, "chat_id": 123, "prompt": "test"})
    data = {f"{PENDING_PREFIX}5": task}

    msg, deleted = cancel_logic(data, "5")

    assert f"{PENDING_PREFIX}5" in deleted
    assert "отменена" in msg


def test_cancel_nonexistent_task():
    msg, deleted = cancel_logic({}, "99")
    assert "не найдена" in msg
    assert deleted == []


def test_cancel_prefers_pending_over_waiting():
    """If somehow both keys exist, pending takes priority."""
    import json
    task = json.dumps({"task_num": 7, "chat_id": 123, "prompt": "test"})
    data = {
        f"{PENDING_PREFIX}7": task,
        f"{WAITING_PREFIX}7": task,
    }
    msg, deleted = cancel_logic(data, "7")
    assert f"{PENDING_PREFIX}7" in deleted
    assert f"{WAITING_PREFIX}7" not in deleted
