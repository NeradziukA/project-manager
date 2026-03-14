import httpx

from shared.config import API_BASE, MAX_MSG


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
