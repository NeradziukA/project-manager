import subprocess
from datetime import datetime
from pathlib import Path

from shared.config import REPO_DIR, PM2_APP_NAME


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


def run_cmd(*args, cwd: Path = REPO_DIR) -> tuple[int, str, str]:
    """Run an arbitrary shell command, return (returncode, stdout, stderr)."""
    result = subprocess.run(
        list(args), cwd=str(cwd),
        capture_output=True, text=True
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()



async def vds_deploy() -> tuple[bool, str]:
    """git add + commit + push origin + build client/server + pm2 restart."""
    rc, status_out, _ = git("status", "--porcelain")
    if status_out:
        git("add", "-A")
        rc, _, err = git("commit", "-m", f"feat: claude code task [{datetime.utcnow().strftime('%H:%M')}]")
        if rc != 0:
            return False, f"git commit failed: {err}"

    rc, _, err = git("push", "origin", "main")
    if rc != 0:
        return False, f"git push origin failed:\n{err}"

    # build client → copy to server/static/
    rc, _, err = run_cmd("npm", "run", "build")
    if rc != 0:
        return False, f"client build failed:\n{err}"

    # compile server TypeScript
    rc, _, err = run_cmd("npm", "run", "build", cwd=REPO_DIR / "server")
    if rc != 0:
        return False, f"server build failed:\n{err}"

    # restart pm2 process
    rc, out, err = run_cmd("npx", "pm2", "restart", PM2_APP_NAME, cwd=REPO_DIR / "server")
    if rc != 0:
        return False, f"pm2 restart failed:\n{err}"

    return True, f"✅ Собран и перезапущен pm2 ({PM2_APP_NAME})"


async def pm2_restart() -> tuple[bool, str]:
    """Just restart the pm2 process without rebuilding."""
    rc, out, err = run_cmd("npx", "pm2", "restart", PM2_APP_NAME, cwd=REPO_DIR / "server")
    if rc != 0:
        return False, f"pm2 restart failed:\n{err}"
    return True, f"✅ pm2 {PM2_APP_NAME} перезапущен"
