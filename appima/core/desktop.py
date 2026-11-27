"""XDG desktop entry and icon integration for Linux / KDE Plasma."""

from __future__ import annotations

import configparser
import difflib
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from appima.core.config import AppimaPaths
from appima.core.models import (
    AppRecord,
    DesktopConflict,
    ExtractedMetadata,
    IntegrationError,
)

MANAGED_MARKER_KEY = "X-Appima-Managed"
MANAGED_ID_KEY = "X-Appima-Id"
_NORM_STRIP_RE = re.compile(r"[^a-z0-9]+")
_TRAILING_NOISE_RE = re.compile(
    r"\b(?:appimage|linux|x86_64|amd64|aarch64|arm64|desktop|v?\d+(?:\.\d+)*)\b",
    re.IGNORECASE,
)


def _quote_desktop_arg(value: str) -> str:
    """Quote an argument according to the Freedesktop Desktop Entry Exec spec."""
    needs_quote = any(ch in value for ch in ' \t\n"\'\\><~|&;$*?#()`')
    if not needs_quote:
        return value
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )
    return f'"{escaped}"'


def build_exec_line(
    exec_path: str | Path,
    launch_args: str = "",
    env_vars: str = "",
    field_code: str = "%U",
) -> str:
    """Build a valid XDG Exec= command line with optional env vars and extra args."""
    parts: list[str] = []

    env_tokens: list[str] = []
    if env_vars.strip():
        try:
            raw_env = shlex.split(env_vars.strip())
        except ValueError as exc:
            raise IntegrationError(f"Invalid environment variables syntax: {exc}") from exc
        for token in raw_env:
            if "=" not in token:
                raise IntegrationError(
                    f"Environment variable {token!r} must be in KEY=VALUE format."
                )
            env_tokens.append(token)

    if env_tokens:
        parts.append("env")
        parts.extend(_quote_desktop_arg(tok) for tok in env_tokens)

    parts.append(_quote_desktop_arg(str(exec_path)))

    if launch_args.strip():
        try:
            extra = shlex.split(launch_args.strip())
        except ValueError as exc:
            raise IntegrationError(f"Invalid launch arguments syntax: {exc}") from exc
        parts.extend(_quote_desktop_arg(tok) for tok in extra)

    if field_code:
        parts.append(field_code)

    return " ".join(parts)


def parse_desktop_metadata(desktop_text: str) -> dict[str, str]:
    """Extract common fields from an embedded .desktop file."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # type: ignore[assignment]
    try:
        parser.read_string(desktop_text)
    except configparser.Error:
        return {}

    if not parser.has_section("Desktop Entry"):
        return {}
    section = parser["Desktop Entry"]
    return {
        "name": section.get("Name", "").strip(),
        "comment": section.get("Comment", "").strip(),
        "categories": section.get("Categories", "").strip(),
        "icon": section.get("Icon", "").strip(),
        "mime_types": section.get("MimeType", "").strip(),
        "wm_class": section.get("StartupWMClass", "").strip(),
        "terminal": section.get("Terminal", "false").strip().lower(),
        "version": section.get("X-AppImage-Version", "").strip(),
    }


def _extract_field_code(desktop_text: str) -> str:
    """Preserve %U / %F / %u / %f field code from an original Exec= line if present."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # type: ignore[assignment]
    try:
        parser.read_string(desktop_text)
        exec_val = parser.get("Desktop Entry", "Exec", fallback="")
    except configparser.Error:
        return "%U"
    for code in ("%U", "%F", "%u", "%f"):
        if code in exec_val:
            return code
    return "%U"


def integrate_app(
    record: AppRecord,
    metadata: ExtractedMetadata,
    paths: AppimaPaths,
    refresh_cache: bool = True,
) -> tuple[Path, Path | None]:
    """Write icon and managed .desktop file, returning (desktop_path, icon_path)."""
    paths.applications_dir.mkdir(parents=True, exist_ok=True)
    paths.icons_dir.mkdir(parents=True, exist_ok=True)

    # Save extracted icon if provided, or keep existing icon on re-integration
    icon_path: Path | None = None
    if metadata.icon_bytes and metadata.icon_ext:
        ext = metadata.icon_ext.lstrip(".").lower()
        # Clean up any previous icon with a different extension
        for old in paths.icons_dir.glob(f"{record.id}.*"):
            old.unlink(missing_ok=True)
        icon_path = paths.icons_dir / f"{record.id}.{ext}"
        icon_path.write_bytes(metadata.icon_bytes)
    elif record.icon_path and Path(record.icon_path).exists():
        icon_path = Path(record.icon_path)

    desktop_path = paths.applications_dir / f"appima-{record.id}.desktop"

    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # type: ignore[assignment]

    if metadata.desktop_content.strip():
        try:
            parser.read_string(metadata.desktop_content)
        except configparser.Error:
            parser.clear()

    if not parser.has_section("Desktop Entry"):
        parser.add_section("Desktop Entry")

    entry = parser["Desktop Entry"]
    entry["Type"] = "Application"
    entry["Name"] = record.name or metadata.name or record.id
    if metadata.comment and not entry.get("Comment"):
        entry["Comment"] = metadata.comment
    if not entry.get("Categories"):
        entry["Categories"] = metadata.categories or "Utility;"
    if metadata.mime_types and not entry.get("MimeType"):
        entry["MimeType"] = metadata.mime_types
    if metadata.wm_class and not entry.get("StartupWMClass"):
        entry["StartupWMClass"] = metadata.wm_class
    if metadata.terminal:
        entry["Terminal"] = "true"
    elif "Terminal" not in entry:
        entry["Terminal"] = "false"

    field_code = (
        _extract_field_code(metadata.desktop_content)
        if metadata.desktop_content
        else "%U"
    )
    entry["Exec"] = build_exec_line(
        record.install_path,
        launch_args=record.launch_args,
        env_vars=record.env_vars,
        field_code=field_code,
    )
    entry["TryExec"] = str(record.install_path)
    entry["Icon"] = str(icon_path) if icon_path else "application-x-executable"
    entry[MANAGED_MARKER_KEY] = "true"
    entry[MANAGED_ID_KEY] = record.id

    # Remove keys that could break AppImage execution in KDE/XDG launchers
    for unwanted in ("Path", "X-AppImage-Integrate"):
        entry.pop(unwanted, None)

    lines: list[str] = []
    for section_name in parser.sections():
        # Skip sub-actions referencing stale internal binary paths from the AppImage
        if section_name != "Desktop Entry":
            continue
        lines.append(f"[{section_name}]")
        for key, value in parser.items(section_name):
            lines.append(f"{key}={value}")
        lines.append("")

    desktop_path.write_text("\n".join(lines), encoding="utf-8")
    desktop_path.chmod(0o644)

    if refresh_cache:
        refresh_desktop_caches(paths)

    return desktop_path, icon_path


def remove_integration(
    record: AppRecord,
    paths: AppimaPaths,
    refresh_cache: bool = True,
) -> None:
    """Safely remove managed .desktop file and extracted icon."""
    if record.desktop_file:
        desktop_path = Path(record.desktop_file)
        if desktop_path.exists():
            try:
                content = desktop_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
            # Safety guard: never delete a non-appima .desktop file
            if f"{MANAGED_MARKER_KEY}=true" in content:
                desktop_path.unlink(missing_ok=True)

    if record.icon_path:
        icon_path = Path(record.icon_path)
        try:
            icon_resolved = icon_path.resolve()
            icons_root = paths.icons_dir.resolve()
            if icon_resolved.parent == icons_root:
                icon_path.unlink(missing_ok=True)
        except OSError:
            pass

    if refresh_cache:
        refresh_desktop_caches(paths)


def refresh_desktop_caches(paths: AppimaPaths) -> None:
    """Notify XDG and KDE Plasma about updated desktop entries."""
    update_db = shutil.which("update-desktop-database")
    if update_db:
        try:
            subprocess.run(
                [update_db, str(paths.applications_dir)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    kbuild = shutil.which("kbuildsycoca6") or shutil.which("kbuildsycoca5")
    if kbuild:
        try:
            subprocess.run(
                [kbuild],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass


def _normalize_app_name(name: str) -> str:
    """Normalize an application name for fuzzy duplicate detection."""
    cleaned = _TRAILING_NOISE_RE.sub(" ", name.lower())
    return _NORM_STRIP_RE.sub("", cleaned).strip()


def _name_similarity(candidate_name: str, existing_name: str) -> float:
    """Compute a similarity score in [0.0, 1.0] between two app names."""
    a = _normalize_app_name(candidate_name)
    b = _normalize_app_name(existing_name)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Substring match for meaningful names (e.g. "Obsidian" vs "Obsidian Notes")
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 4 and shorter in longer:
        return max(0.85, len(shorter) / len(longer))
    return difflib.SequenceMatcher(None, a, b).ratio()


def _classify_desktop_origin(desktop_path: Path, is_managed: bool) -> str:
    if is_managed:
        return "appima"
    s = str(desktop_path)
    if "/flatpak/" in s:
        return "Flatpak"
    if "/snapd/" in s or "/snap/" in s:
        return "Snap"
    if str(desktop_path).startswith(str(Path.home())):
        return "Local user entry"
    return "System"


def default_application_dirs(paths: AppimaPaths) -> list[Path]:
    """Return standard XDG, Flatpak, and Snap application directories on Linux."""
    candidates: list[Path] = [
        paths.applications_dir,
        Path.home() / ".local" / "share" / "flatpak" / "exports" / "share" / "applications",
        Path("/usr/local/share/applications"),
        Path("/usr/share/applications"),
        Path("/var/lib/flatpak/exports/share/applications"),
        Path("/var/lib/snapd/desktop/applications"),
    ]
    for raw in os.environ.get("XDG_DATA_DIRS", "").split(":"):
        raw = raw.strip()
        if raw:
            candidates.append(Path(raw) / "applications")

    seen: set[Path] = set()
    unique: list[Path] = []
    for d in candidates:
        try:
            resolved = d.expanduser().resolve()
        except OSError:
            resolved = d
        if resolved not in seen and resolved.is_dir():
            seen.add(resolved)
            unique.append(resolved)
    return unique


def find_existing_desktop_entries(
    app_name: str,
    paths: AppimaPaths,
    exclude_app_id: str = "",
    search_dirs: list[Path] | None = None,
    threshold: float = 0.78,
) -> list[DesktopConflict]:
    """Scan installed .desktop files for exact or closely similar application names."""
    if not app_name.strip():
        return []

    dirs = search_dirs if search_dirs is not None else default_application_dirs(paths)
    conflicts: list[DesktopConflict] = []
    seen_paths: set[str] = set()

    for app_dir in dirs:
        if not app_dir.is_dir():
            continue
        for desktop_file in sorted(app_dir.glob("*.desktop")):
            if not desktop_file.is_file():
                continue
            path_key = str(desktop_file)
            if path_key in seen_paths:
                continue
            seen_paths.add(path_key)

            try:
                text = desktop_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            parser = configparser.ConfigParser(interpolation=None, strict=False)
            parser.optionxform = str  # type: ignore[assignment]
            try:
                parser.read_string(text)
            except configparser.Error:
                continue

            if not parser.has_section("Desktop Entry"):
                continue
            entry = parser["Desktop Entry"]
            if entry.get("Type", "").strip() != "Application":
                continue
            if entry.get("NoDisplay", "").strip().lower() == "true":
                continue
            if entry.get("Hidden", "").strip().lower() == "true":
                continue

            is_managed = entry.get(MANAGED_MARKER_KEY, "").strip().lower() == "true"
            managed_id = entry.get(MANAGED_ID_KEY, "").strip()
            if exclude_app_id and is_managed and managed_id == exclude_app_id:
                continue

            entry_name = entry.get("Name", "").strip()
            if not entry_name:
                continue

            score = _name_similarity(app_name, entry_name)
            if score >= threshold:
                conflicts.append(
                    DesktopConflict(
                        name=entry_name,
                        desktop_path=str(desktop_file),
                        exec_cmd=entry.get("Exec", "").strip(),
                        source_kind=_classify_desktop_origin(desktop_file, is_managed),
                        similarity=score,
                        is_appima_managed=is_managed,
                    )
                )

    conflicts.sort(key=lambda c: (-c.similarity, c.name.lower()))
    return conflicts
