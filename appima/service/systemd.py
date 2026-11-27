"""Manage a lightweight systemd --user timer for background update checks."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from appima.core.config import AppimaPaths
from appima.core.models import VALID_CHECK_INTERVALS

SERVICE_NAME = "appima-check.service"
TIMER_NAME = "appima-check.timer"

_TIMER_RULES = {
    "6h": "OnBootSec=15m\nOnUnitActiveSec=6h\nRandomizedDelaySec=10m",
    "daily": "OnCalendar=daily\nPersistent=true\nRandomizedDelaySec=15m",
    "weekly": "OnCalendar=weekly\nPersistent=true\nRandomizedDelaySec=30m",
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def render_service_unit(python_executable: str | None = None) -> str:
    py = python_executable or sys.executable
    root = _repo_root()
    return (
        "[Unit]\n"
        "Description=appima background update check\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"Environment=PYTHONPATH={root}\n"
        f"ExecStart={py} -m appima check --notify\n"
    )


def render_timer_unit(interval: str) -> str:
    rule = _TIMER_RULES.get(interval)
    if not rule:
        raise ValueError(f"Unsupported timer interval: {interval!r}")
    return (
        "[Unit]\n"
        "Description=Periodic update check for appima managed apps\n"
        "\n"
        "[Timer]\n"
        f"{rule}\n"
        f"Unit={SERVICE_NAME}\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def _run_systemctl(*args: str) -> tuple[bool, str]:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, "systemctl not found"
    try:
        proc = subprocess.run(
            [systemctl, "--user", *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        msg = (proc.stderr or proc.stdout).strip()
        return proc.returncode == 0, msg
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def sync_update_timer(
    interval: str,
    paths: AppimaPaths,
    run_systemctl: bool = True,
) -> tuple[bool, str]:
    """Install, update, or remove the systemd --user timer for background checks."""
    if interval not in VALID_CHECK_INTERVALS:
        raise ValueError(f"Invalid check interval: {interval!r}")

    unit_dir = paths.systemd_user_dir
    service_path = unit_dir / SERVICE_NAME
    timer_path = unit_dir / TIMER_NAME

    if interval == "off":
        if run_systemctl:
            _run_systemctl("disable", "--now", TIMER_NAME)
        timer_path.unlink(missing_ok=True)
        service_path.unlink(missing_ok=True)
        if run_systemctl:
            _run_systemctl("daemon-reload")
        return True, "Background update checks disabled."

    unit_dir.mkdir(parents=True, exist_ok=True)
    service_path.write_text(render_service_unit(), encoding="utf-8")
    timer_path.write_text(render_timer_unit(interval), encoding="utf-8")

    if not run_systemctl:
        return True, f"Timer unit written ({interval})."

    ok_reload, msg_reload = _run_systemctl("daemon-reload")
    if not ok_reload:
        return False, f"Units written, but systemd user daemon unavailable: {msg_reload}"

    ok_enable, msg_enable = _run_systemctl("enable", "--now", TIMER_NAME)
    if not ok_enable:
        return False, f"Units written, but could not enable timer: {msg_enable}"

    return True, f"Background update timer active ({interval})."


def get_timer_status(paths: AppimaPaths) -> str:
    """Return a concise human-readable status string for the Settings dialog."""
    timer_path = paths.systemd_user_dir / TIMER_NAME
    if not timer_path.is_file():
        return "Off"
    ok, out = _run_systemctl("is-active", TIMER_NAME)
    if ok and out.strip() == "active":
        return "Active (systemd user timer)"
    return "Configured (systemd user session inactive)"
