import asyncio
import shutil
from pathlib import Path

from shared.config import REPO_DIR, CLAUDE_TIMEOUT, QUESTION_INSTRUCTION


def find_claude() -> str:
    path = shutil.which("claude")
    if path:
        return path
    for c in ["/usr/local/bin/claude",
               str(Path.home() / ".npm-global/bin/claude"),
               str(Path.home() / ".local/bin/claude")]:
        if Path(c).exists():
            return c
    raise FileNotFoundError("Claude Code CLI not found. Install: npm install -g @anthropic-ai/claude-code")


# Phrases Claude outputs when the API rate limit is hit
_RATE_LIMIT_PHRASES = (
    "you've hit your limit",
    "you have hit your limit",
    "hit your limit",
)


def is_rate_limited(text: str) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in _RATE_LIMIT_PHRASES)


async def run_claude(prompt: str) -> tuple[bool, str]:
    """Run claude --print in the repo directory.
    Returns (success, output).
    """
    claude_bin = find_claude()
    full_prompt = QUESTION_INSTRUCTION + prompt
    try:
        proc = await asyncio.create_subprocess_exec(
            claude_bin, "--print", "--dangerously-skip-permissions",
            "--output-format", "text", full_prompt,
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
