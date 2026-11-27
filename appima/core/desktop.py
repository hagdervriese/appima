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
    RemovalPlan,
)
from appima.core.runtime import clean_subprocess_env

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
    icons_dir: Path | None = None,
) -> tuple[Path, Path | None]:
    """Write icon into icons_dir (default: <apps_dir>/.icons) and managed .desktop file."""
    target_icons_dir = icons_dir or paths.icons_dir
    paths.applications_dir.mkdir(parents=True, exist_ok=True)
    target_icons_dir.mkdir(parents=True, exist_ok=True)

    # Save extracted icon if provided, or keep existing icon on re-integration
    icon_path: Path | None = None
    if metadata.icon_bytes and metadata.icon_ext:
        ext = metadata.icon_ext.lstrip(".").lower()
        # Clean up any previous icon with a different extension (in both current and legacy dirs)
        for old in target_icons_dir.glob(f"{record.id}.*"):
            old.unlink(missing_ok=True)
        if target_icons_dir.resolve() != paths.icons_dir.resolve() and paths.icons_dir.is_dir():
            for legacy in paths.icons_dir.glob(f"{record.id}.*"):
                legacy.unlink(missing_ok=True)
        icon_path = target_icons_dir / f"{record.id}.{ext}"
        icon_path.write_bytes(metadata.icon_bytes)
    elif record.icon_path and Path(record.icon_path).exists():
        existing_icon = Path(record.icon_path)
        # If migrating a legacy icon from ~/.local/share/appima/icons to <apps_dir>/.icons, move it cleanly
        try:
            if (
                existing_icon.resolve().parent == paths.icons_dir.resolve()
                and target_icons_dir.resolve() != paths.icons_dir.resolve()
            ):
                migrated = target_icons_dir / existing_icon.name
                shutil.copy2(existing_icon, migrated)
                existing_icon.unlink(missing_ok=True)
                icon_path = migrated
            else:
                icon_path = existing_icon
        except OSError:
            icon_path = existing_icon

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
    exec_target = record.effective_exec_path
    entry["Exec"] = build_exec_line(
        exec_target,
        launch_args=record.launch_args,
        env_vars=record.env_vars,
        field_code=field_code,
    )
    entry["TryExec"] = str(exec_target)
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
    icons_dir: Path | None = None,
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
        allowed_roots = [paths.icons_dir.resolve()]
        if icons_dir is not None:
            allowed_roots.append(icons_dir.resolve())
        try:
            icon_resolved = icon_path.resolve()
            if icon_resolved.parent in allowed_roots or icon_resolved.parent.name == ".icons":
                icon_path.unlink(missing_ok=True)
        except OSError:
            pass

    if refresh_cache:
        refresh_desktop_caches(paths)


def refresh_desktop_caches(paths: AppimaPaths) -> None:
    """Notify XDG and KDE Plasma about updated desktop entries."""
    sub_env = clean_subprocess_env()
    update_db = shutil.which("update-desktop-database")
    if update_db:
        try:
            subprocess.run(
                [update_db, str(paths.applications_dir)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=sub_env,
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
                env=sub_env,
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
                origin = _classify_desktop_origin(desktop_file, is_managed)
                flatpak_id = entry.get("X-Flatpak", "").strip()
                if not flatpak_id and origin == "Flatpak":
                    flatpak_id = desktop_file.stem
                conflicts.append(
                    DesktopConflict(
                        name=entry_name,
                        desktop_path=str(desktop_file),
                        exec_cmd=entry.get("Exec", "").strip(),
                        icon_value=entry.get("Icon", "").strip(),
                        source_kind=origin,
                        similarity=score,
                        is_appima_managed=is_managed,
                        managed_app_id=managed_id if is_managed else "",
                        flatpak_id=flatpak_id,
                    )
                )

    conflicts.sort(key=lambda c: (-c.similarity, c.name.lower()))
    return conflicts


def has_exact_desktop_name_conflict(
    app_name: str,
    paths: AppimaPaths,
    exclude_app_id: str = "",
    search_dirs: list[Path] | None = None,
) -> DesktopConflict | None:
    """Return an existing desktop entry whose visible Name matches app_name exactly."""
    target = app_name.strip().lower()
    if not target:
        return None
    for conflict in find_existing_desktop_entries(
        app_name,
        paths,
        exclude_app_id=exclude_app_id,
        search_dirs=search_dirs,
        threshold=0.9,
    ):
        if conflict.name.strip().lower() == target:
            return conflict
    return None


def suggest_unique_app_name(
    base_name: str,
    version: str,
    paths: AppimaPaths,
    search_dirs: list[Path] | None = None,
) -> str:
    """Suggest a clean, unique desktop display name (preferring the app version)."""
    clean_base = base_name.strip() or "App"
    candidates: list[str] = []
    if version and version not in ("unknown", "local"):
        candidates.append(f"{clean_base} ({version})")
    candidates.append(f"{clean_base} (AppImage)")
    for n in range(2, 20):
        candidates.append(f"{clean_base} ({n})")

    for cand in candidates:
        if not has_exact_desktop_name_conflict(cand, paths, search_dirs=search_dirs):
            return cand
    return f"{clean_base} (New)"


def _extract_exec_binary(exec_cmd: str) -> Path | None:
    """Parse the primary executable path from an XDG Exec= line."""
    if not exec_cmd.strip():
        return None
    try:
        tokens = shlex.split(exec_cmd.strip())
    except ValueError:
        tokens = exec_cmd.strip().split()
    if not tokens:
        return None

    idx = 0
    if tokens[0] in ("env", "/usr/bin/env"):
        idx = 1
        while idx < len(tokens) and "=" in tokens[idx]:
            idx += 1
    if idx >= len(tokens):
        return None

    raw_cmd = tokens[idx]
    if raw_cmd.startswith(("/", "~")):
        return Path(raw_cmd).expanduser()
    return None


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def plan_conflict_removal(
    conflict: DesktopConflict,
    paths: AppimaPaths,
    apps_dir: Path,
    home_dir: Path | None = None,
) -> RemovalPlan:
    """Build a safe, explicit removal plan for a conflicting desktop entry."""
    home = (home_dir or Path.home()).resolve()
    desktop_file = Path(conflict.desktop_path)

    # 1. Managed by appima itself
    if conflict.is_appima_managed and conflict.managed_app_id:
        return RemovalPlan(
            can_remove=True,
            method="appima",
            summary=f"Uninstall appima-managed app \"{conflict.name}\"",
            managed_app_id=conflict.managed_app_id,
        )

    # 2. Flatpak app
    if conflict.source_kind == "Flatpak" and conflict.flatpak_id:
        if not shutil.which("flatpak"):
            return RemovalPlan(
                can_remove=False,
                method="unsupported",
                summary="",
                reason="The 'flatpak' command is not available on this system.",
            )
        scope = "--user" if _is_under(desktop_file, home) else "--system"
        return RemovalPlan(
            can_remove=True,
            method="flatpak",
            summary=f"Uninstall Flatpak \"{conflict.name}\" ({conflict.flatpak_id}, {scope})",
            flatpak_id=conflict.flatpak_id,
            flatpak_scope=scope,
        )

    # 3. Local user-space sideloaded .desktop entry
    if conflict.source_kind == "Local user entry" and _is_under(desktop_file, home):
        to_delete: list[Path] = []
        if desktop_file.is_file() and desktop_file.suffix == ".desktop":
            to_delete.append(desktop_file)

        # Dedicated portable app parent roots where a single child folder can be removed safely
        safe_app_roots = [
            apps_dir.expanduser().resolve(),
            (home / "Apps").resolve(),
            (home / "Applications").resolve(),
            (home / ".local" / "opt").resolve(),
            (home / "opt").resolve(),
        ]
        safe_bin_dirs = [
            (home / ".local" / "bin").resolve(),
            (home / "bin").resolve(),
        ]
        safe_icon_roots = [
            (home / ".local" / "share" / "icons").resolve(),
            (home / ".local" / "share" / "pixmaps").resolve(),
            (apps_dir.expanduser() / ".icons").resolve(),
            paths.icons_dir.resolve(),
        ]

        exec_bin = _extract_exec_binary(conflict.exec_cmd)
        if exec_bin and exec_bin.exists() and _is_under(exec_bin, home):
            exec_resolved = exec_bin.resolve()
            added_exec = False
            # Case A: executable lives inside a dedicated subfolder of ~/Apps, ~/Applications, ~/.local/opt, ~/opt
            for app_root in safe_app_roots:
                if _is_under(exec_resolved, app_root) and exec_resolved != app_root:
                    rel = exec_resolved.relative_to(app_root)
                    top_entry = app_root / rel.parts[0]
                    if top_entry.is_dir():
                        to_delete.append(top_entry)
                        added_exec = True
                        break
                    if top_entry.is_file() and top_entry.suffix.lower() == ".appimage":
                        to_delete.append(top_entry)
                        added_exec = True
                        break
            # Case B: standalone .AppImage file anywhere inside home
            if not added_exec and exec_resolved.is_file() and exec_resolved.suffix.lower() == ".appimage":
                to_delete.append(exec_resolved)
                added_exec = True
            # Case C: single binary directly inside ~/.local/bin or ~/bin (never delete bin/ itself)
            if not added_exec and exec_resolved.is_file():
                for bin_dir in safe_bin_dirs:
                    if exec_resolved.parent == bin_dir:
                        to_delete.append(exec_resolved)
                        break

        # Remove user icon file if it's an absolute path inside user icon directories
        if conflict.icon_value.startswith(("/", "~")):
            icon_p = Path(conflict.icon_value).expanduser()
            if icon_p.is_file() and _is_under(icon_p, home):
                if any(_is_under(icon_p, r) for r in safe_icon_roots):
                    if icon_p not in to_delete:
                        to_delete.append(icon_p)

        if to_delete:
            return RemovalPlan(
                can_remove=True,
                method="local_files",
                summary=f"Remove obsolete local entry \"{conflict.name}\"",
                paths_to_delete=to_delete,
            )

    # 4. System / immutable / Snap entries
    return RemovalPlan(
        can_remove=False,
        method="unsupported",
        summary="",
        reason=(
            f"{conflict.source_kind} entries ({conflict.desktop_path}) are managed by "
            "the operating system and cannot be removed by appima."
        ),
    )


def execute_local_removal_plan(
    plan: RemovalPlan,
    paths: AppimaPaths,
    refresh_cache: bool = True,
) -> None:
    """Execute a 'local_files' or 'flatpak' RemovalPlan safely."""
    if not plan.can_remove:
        raise IntegrationError(plan.reason or "This entry cannot be removed by appima.")

    if plan.method == "flatpak":
        flatpak_bin = shutil.which("flatpak")
        if not flatpak_bin:
            raise IntegrationError("The 'flatpak' command is not installed.")
        proc = subprocess.run(
            [flatpak_bin, "uninstall", plan.flatpak_scope, "-y", plan.flatpak_id],
            capture_output=True,
            text=True,
            env=clean_subprocess_env(),
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip()
            raise IntegrationError(
                f"Flatpak uninstall failed ({plan.flatpak_id}): {err or 'permission denied'}"
            )
        if refresh_cache:
            refresh_desktop_caches(paths)
        return

    if plan.method == "local_files":
        for target in plan.paths_to_delete:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink(missing_ok=True)
        if refresh_cache:
            refresh_desktop_caches(paths)
        return

    raise IntegrationError(f"Unsupported removal method: {plan.method}")
