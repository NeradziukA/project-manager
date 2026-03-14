import subprocess
from datetime import datetime
from pathlib import Path

from shared.config import REPO_DIR, GIT_REMOTE


def git(*args, cwd: Path = REPO_DIR) -> tuple[int, str, str]:
    """Run a git command, return (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["git", *args], cwd=str(cwd),
        capture_output=True, text=True
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def get_diff() -> str:
    """Get git diff stat of the last commit (what Claude Code changed)."""
    _, diff, _ = git("diff", "HEAD~1", "HEAD", "--stat")
    return diff or "нет изменений"


async def heroku_deploy() -> tuple[bool, str]:
    """git add + commit + push origin + push heroku."""
    rc, status_out, _ = git("status", "--porcelain")
    if status_out:
        git("add", "-A")
        rc, _, err = git("commit", "-m", f"feat: claude code task [{datetime.utcnow().strftime('%H:%M')}]")
        if rc != 0:
            return False, f"git commit failed: {err}"
    else:
        return True, "Нет изменений файлов — деплой не нужен"

    rc, out, err = git("push", "origin", "main")
    if rc != 0:
        return False, f"git push origin failed:\n{err}"

    rc, out, err = git("push", GIT_REMOTE, "main")
    if rc != 0:
        rc, out, err = git("push", GIT_REMOTE, "master")
    if rc != 0:
        return False, f"git push heroku failed:\n{err}"

    return True, "Push успешен, Heroku собирает..."
