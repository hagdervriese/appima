"""Runtime environment helpers (detecting AppImage vs source execution)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

SELF_APP_ID = "appima"
SELF_GITHUB_REPO = "https://github.com/hagdervriese/appima"


def current_appimage_path() -> Path | None:
    """Return the path of the running AppImage binary, or None when running from source."""
    raw = os.environ.get("APPIMAGE", "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_file() else None


def current_appdir() -> Path | None:
    """Return the mounted AppDir path when running inside an AppImage."""
    raw = os.environ.get("APPDIR", "").strip()
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_dir() else None


def is_frozen() -> bool:
    """Return True if running from a bundled binary (AppImage / PyInstaller)."""
    return bool(current_appimage_path() or getattr(sys, "frozen", False))


def gui_asset_path(filename: str) -> Path:
    """Resolve a GUI asset path both when running from source and inside PyInstaller."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / "appima" / "gui" / "assets" / filename
        if candidate.is_file():
            return candidate
    return Path(__file__).resolve().parent.parent / "gui" / "assets" / filename
