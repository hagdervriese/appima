"""Native Freedesktop / KDE Plasma desktop notification helper."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from appima.core.models import UpdateCheckResult

_ICON_PATH = Path(__file__).resolve().parent.parent / "gui" / "assets" / "appima.svg"


def notify_updates(updates: list[UpdateCheckResult]) -> bool:
    """Send a single calm desktop notification summarizing available updates."""
    ready = [u for u in updates if u.update_available]
    if not ready:
        return False

    notify_send = shutil.which("notify-send")
    if not notify_send:
        return False

    count = len(ready)
    if count == 1:
        item = ready[0]
        title = f"Update available for {item.app_name}"
        body = (
            f"Version {item.installed_version} → {item.available_version}\n"
            "Open appima to install the update."
        )
    else:
        title = f"{count} app updates available"
        preview = "\n".join(
            f"• {u.app_name} ({u.installed_version} → {u.available_version})"
            for u in ready[:5]
        )
        if count > 5:
            preview += f"\n• and {count - 5} more…"
        body = f"{preview}\nOpen appima to review and update."

    icon_arg = str(_ICON_PATH) if _ICON_PATH.is_file() else "system-software-update"
    cmd = [
        notify_send,
        "--app-name=appima",
        f"--icon={icon_arg}",
        "--urgency=normal",
        title,
        body,
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
