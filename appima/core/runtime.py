"""Runtime environment helpers (detecting AppImage vs source execution)."""

from __future__ import annotations

import os
import subprocess
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


def clean_subprocess_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Strip AppImage/PyInstaller mount variables so a relaunched AppImage starts cleanly."""
    env = dict(base_env if base_env is not None else os.environ)
    appdir = env.get("APPDIR", "")
    meipass = str(getattr(sys, "_MEIPASS", "") or "")

    if "LD_LIBRARY_PATH_ORIG" in env:
        orig = env.pop("LD_LIBRARY_PATH_ORIG")
        if orig:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
    elif "LD_LIBRARY_PATH" in env:
        parts = [
            p
            for p in env["LD_LIBRARY_PATH"].split(":")
            if p
            and (not appdir or not p.startswith(appdir))
            and (not meipass or not p.startswith(meipass))
        ]
        if parts:
            env["LD_LIBRARY_PATH"] = ":".join(parts)
        else:
            env.pop("LD_LIBRARY_PATH", None)

    if appdir and "PATH" in env:
        parts = [p for p in env["PATH"].split(":") if p and not p.startswith(appdir)]
        env["PATH"] = ":".join(parts)

    for var in (
        "APPIMAGE",
        "APPDIR",
        "OWD",
        "ARGV0",
        "_MEIPASS",
        "_MEIPASS2",
        "PYTHONHOME",
    ):
        env.pop(var, None)
    return env


def relaunch_appima(binary_path: Path | str | None = None) -> None:
    """Spawn a detached fresh instance of appima (AppImage or source)."""
    env = clean_subprocess_env()
    target = Path(binary_path).expanduser() if binary_path else current_appimage_path()
    if target and target.is_file():
        cmd = [str(target)]
    else:
        cmd = [sys.executable, "-m", "appima"]
    subprocess.Popen(
        cmd,
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
