"""
worker.py — воркер:
  1. Берёт задачу из Redis
  2. git pull (обновляет репо)
  3. Запускает Claude Code CLI с промптом
  4. git add + commit + push heroku
  5. Отправляет результат в Telegram
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

# ── конфиг ────────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.environ["TELEGRAM_BOT_TOKEN"]
REDIS_URL       = os.getenv("REDIS_URL", "redis://localhost:6379")
TASK_QUEUE      = os.getenv("TASK_QUEUE", "claude:tasks")
RESULT_KEY      = os.getenv("RESULT_KEY", "claude:last_result")
FAILED_QUEUE    = os.getenv("FAILED_QUEUE", "claude:failed")

REPO_DIR        = Path(os.environ["REPO_DIR"])
GIT_REMOTE      = os.getenv("HEROKU_GIT_REMOTE", "heroku")

CLAUDE_TIMEOUT  = int(os.getenv("CLAUDE_TIMEOUT", "300"))
API_BASE        = f"https://api.telegram.org/bot{BOT_TOKEN}"

MAX_MSG         = 3800   # символов в сообщении Telegram


# ── Telegram ──────────────────────────────────────────────────────────────────
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


# ── git helpers ───────────────────────────────────────────────────────────────
def git(*args, cwd: Path = REPO_DIR) -> tuple[int, str, str]:
    """Запустить git команду, вернуть (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["git", *args], cwd=str(cwd),
        capture_output=True, text=True
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def get_diff() -> str:
    """Получить git diff последнего коммита (что изменил Claude Code)."""
    _, diff, _ = git("diff", "HEAD~1", "HEAD", "--stat")
    return diff or "нет изменений"


def get_commit_hash() -> str:
    _, h, _ = git("rev-parse", "--short", "HEAD")
    return h


# ── Claude Code ───────────────────────────────────────────────────────────────
def find_claude() -> str:
    path = shutil.which("claude")
    if path:
        return path
    for c in ["/usr/local/bin/claude",
               str(Path.home() / ".npm-global/bin/claude"),
               str(Path.home() / ".local/bin/claude")]:
        if Path(c).exists():
            return c
    raise FileNotFoundError("Claude Code CLI не найден. Установи: npm install -g @anthropic-ai/claude-code")


async def run_claude(prompt: str) -> tuple[bool, str]:
    """Запустить claude --print в папке репозитория."""
    claude_bin = find_claude()
    try:
        proc = await asyncio.create_subprocess_exec(
            claude_bin, "--print", "--output-format", "text", prompt,
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


# ── Heroku deploy ─────────────────────────────────────────────────────────────
async def heroku_deploy() -> tuple[bool, str]:
    """
    git push heroku main, вернуть (success, статус).
    """
    # 1. commit если есть изменения
    rc, status_out, _ = git("status", "--porcelain")
    if status_out:
        git("add", "-A")
        rc, _, err = git("commit", "-m", f"feat: claude code task [{datetime.utcnow().strftime('%H:%M')}]")
        if rc != 0:
            return False, f"git commit failed: {err}"
    else:
        return True, "Нет изменений файлов — деплой не нужен"

    # 2. push на heroku
    branch = "main"
    rc, out, err = git("push", GIT_REMOTE, branch)
    if rc != 0:
        # попробовать master
        rc, out, err = git("push", GIT_REMOTE, "master")
    if rc != 0:
        return False, f"git push failed:\n{err}"

    return True, "Push успешен, Heroku собирает..."



# ── основной цикл ─────────────────────────────────────────────────────────────
async def process_task(redis_client: aioredis.Redis, task_raw: str):
    task       = json.loads(task_raw)
    task_id    = task["task_id"]
    prompt     = task["prompt"]
    chat_id    = task["chat_id"]
    message_id = task.get("message_id")
    ack_id     = task.get("ack_msg_id")

    log.info("Task %s: %s", task_id, prompt[:80])
    started = datetime.utcnow()

    async with httpx.AsyncClient(timeout=30) as client:

        # ── шаг 1: git pull ───────────────────────────────────────────────────
        if ack_id:
            await tg_edit(client, chat_id, ack_id,
                          "🔄 *Шаг 1/3* — Обновляю репозиторий...")
        rc, _, err = git("pull", "--rebase")
        if rc != 0:
            log.warning("git pull failed: %s", err)

        # ── шаг 2: Claude Code ────────────────────────────────────────────────
        if ack_id:
            await tg_edit(client, chat_id, ack_id,
                          "🤖 *Шаг 2/3* — Claude Code выполняет задачу...\n\nЭто может занять несколько минут.")

        claude_ok, claude_out = await run_claude(prompt)
        elapsed = round((datetime.utcnow() - started).total_seconds(), 1)

        # ── шаг 3: деплой на Heroku ───────────────────────────────────────────
        deploy_status = "не выполнялся"
        if claude_ok:
            if ack_id:
                await tg_edit(client, chat_id, ack_id,
                              "🚀 *Шаг 3/3* — Деплой на Heroku...")
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

        # ── итоговое сообщение ────────────────────────────────────────────────
        icon = "✅" if claude_ok else "❌"
        header = (
            f"{icon} *Задача выполнена* (⏱ {elapsed}с)\n\n"
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

    # ── сохранить для /status ─────────────────────────────────────────────────
    await redis_client.set(RESULT_KEY, json.dumps({
        "task_id"       : task_id,
        "prompt"        : prompt,
        "success"       : claude_ok,
        "elapsed"       : elapsed,
        "deploy_status" : deploy_status,
        "finished"      : datetime.utcnow().isoformat(),
    }, ensure_ascii=False))


async def main():
    log.info("Worker started. Repo: %s", REPO_DIR)
    redis_client = await aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        while True:
            item = await redis_client.blpop(TASK_QUEUE, timeout=2)
            if item is None:
                continue
            _, raw = item
            try:
                await process_task(redis_client, raw)
            except Exception as e:
                log.exception("Task failed: %s", e)
                await redis_client.rpush(FAILED_QUEUE, raw)
    finally:
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
