import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN       = os.environ["TELEGRAM_BOT_TOKEN"]
REDIS_URL       = os.getenv("REDIS_URL",       "redis://localhost:6379")
TASK_QUEUE      = os.getenv("TASK_QUEUE",      "claude:tasks")
RESULT_KEY      = os.getenv("RESULT_KEY",      "claude:last_result")
FAILED_QUEUE    = os.getenv("FAILED_QUEUE",    "claude:failed")
WAITING_PREFIX  = os.getenv("WAITING_PREFIX",  "claude:waiting:")
PROGRESS_KEY    = os.getenv("PROGRESS_KEY",    "claude:in_progress")
PENDING_PREFIX  = os.getenv("PENDING_PREFIX",  "claude:pending:")
TASK_COUNTER    = os.getenv("TASK_COUNTER",    "claude:task_counter")
NOTIFY_QUEUE    = os.getenv("NOTIFY_QUEUE",    "claude:notify")

ALLOWED_IDS     = set(int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip())
ALERT_CHAT_ID   = int(os.getenv("ALERT_CHAT_ID", "0")) or next(iter(ALLOWED_IDS), None)
NOTIFY_CHAT_ID  = int(os.getenv("NOTIFY_CHAT_ID", "0")) or next(iter(ALLOWED_IDS), None)
CHECK_INTERVAL  = int(os.getenv("WORKER_CHECK_INTERVAL", "300"))

REPO_DIR        = Path(os.environ["REPO_DIR"])
PM2_APP_NAME    = os.getenv("PM2_APP_NAME", "hives")

CLAUDE_TIMEOUT     = int(os.getenv("CLAUDE_TIMEOUT", "300"))
HEARTBEAT_KEY      = os.getenv("HEARTBEAT_KEY",   "claude:worker:heartbeat")
HEARTBEAT_TTL      = 30   # seconds until key expires — if missing, worker is down
HEARTBEAT_INTERVAL = 10   # how often to refresh

API_BASE        = f"https://api.telegram.org/bot{BOT_TOKEN}"

MAX_MSG         = 3800   # chars per Telegram message

QUESTION_MARKER = "QUESTION:"

QUESTION_INSTRUCTION = (
    "IMPORTANT SYSTEM RULE: You have full permissions to read and write all files — "
    "never ask for file write permissions. "
    "If at ANY point — before, during, or after partial work — you need a decision or value from the user, "
    f"you MUST end your entire response with exactly: {QUESTION_MARKER} [your question] "
    "Do NOT ask questions in plain text. Do NOT mix explanations with a question — "
    f"if you have a question, your last line must be {QUESTION_MARKER} followed by the question and nothing after. "
    "Otherwise, proceed and complete the task fully.\n\n"
)
