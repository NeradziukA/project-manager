import httpx

from shared.config import API_BASE


async def tg(method: str, **kwargs) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{API_BASE}/{method}", json=kwargs)
        return r.json()


async def send(chat_id: int, text: str,
               reply_to: int | None = None,
               reply_markup: dict | None = None) -> dict:
    kwargs = {"chat_id": chat_id, "text": text[:4096], "parse_mode": "Markdown"}
    if reply_to:
        kwargs["reply_to_message_id"] = reply_to
    if reply_markup:
        kwargs["reply_markup"] = reply_markup
    return await tg("sendMessage", **kwargs)


async def edit(chat_id: int, msg_id: int, text: str,
               reply_markup: dict | None = None) -> dict:
    kwargs = {
        "chat_id": chat_id, "message_id": msg_id,
        "text": text[:4096], "parse_mode": "Markdown",
    }
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    return await tg("editMessageText", **kwargs)


def confirm_keyboard(task_num: int) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Запустить",  "callback_data": f"ok_{task_num}"},
            {"text": "🚫 Отменить",  "callback_data": f"cancel_{task_num}"},
        ]]
    }
