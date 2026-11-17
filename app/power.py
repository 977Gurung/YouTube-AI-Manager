"""Restart / shut down the server from the dashboard (System tab).

The API runs as your normal user, so it may only run the exact commands that the installer allows in
/etc/sudoers.d/ytm-power (see scripts/setup-power.sh): reboot, power off, and restarting its own two
services. Nothing else gets root. The command starts 2 seconds after the request so the browser still
gets its answer.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time

from .config import settings

log = logging.getLogger("ytm.power")
SYSTEMCTL = "/usr/bin/systemctl"
COMMANDS = {
    "reboot": [SYSTEMCTL, "--no-block", "reboot"],
    "shutdown": [SYSTEMCTL, "--no-block", "poweroff"],
    "restart-app": [SYSTEMCTL, "--no-block", "restart", "ytm-api.service", "ytm-n8n.service"],
}
_cache: dict = {"t": 0.0, "value": None}


def _sudo_allows(cmd: list[str]) -> bool:
    if not shutil.which("sudo"):
        return False
    try:
        return subprocess.run(["sudo", "-n", "-l", *cmd], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def status() -> dict:
    if not settings.get("power.enabled", True):
        return {"available": False, "actions": [], "reason": "turned off in settings.yaml (power.enabled)"}
    if time.time() - _cache["t"] < 60 and _cache["value"] is not None:
        return _cache["value"]
    actions = [a for a, cmd in COMMANDS.items() if _sudo_allows(cmd)]
    value = {"available": bool(actions), "actions": actions,
             "reason": "" if actions else "not set up yet - run ./scripts/setup-power.sh on the server (or upgrade)"}
    _cache.update(t=time.time(), value=value)
    return value


def run(action: str, delay: float = 2.0) -> None:
    cmd = ["sudo", "-n", *COMMANDS[action]]

    def go():
        time.sleep(delay)
        log.warning("power: %s", " ".join(cmd))
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if p.returncode != 0:
                log.error("power %s failed: %s", action, (p.stderr or p.stdout).strip()[:300])
        except (OSError, subprocess.SubprocessError) as e:
            log.error("power %s failed: %s", action, e)
    threading.Thread(target=go, name=f"ytm-power-{action}", daemon=True).start()
