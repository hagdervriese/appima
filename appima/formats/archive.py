"""Portable archive bundle handler (.tar.gz, .tar.xz, .tar.bz2, .zip, and extracted dirs).

Handles self-contained Linux directory bundles such as Antigravity (Electron),
Android Studio & JetBrains IDEs (product-info.json), VS Code tarballs, and
generic portable archive apps.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import struct
import tarfile
import tempfile
import zipfile
from pathlib import Path

from appima.core.desktop import parse_desktop_metadata
from appima.core.models import ExtractedMetadata, FormatError
from appima.formats.base import PackageFormat

ARCHIVE_SUFFIXES = (
    ".tar.gz",
    ".tgz",
    ".tar.xz",
    ".txz",
    ".tar.bz2",
    ".tbz2",
    ".tar",
    ".zip",
)

_ELF_MAGIC = b"\x7fELF"
_VERSION_IN_NAME_RE = re.compile(
    r"[-_ ]v?(\d+\.\d+(?:\.\d+)?(?:[-.](?:alpha|beta|rc|pre|dev|nightly|post)[0-9A-Za-z.]*)?)",
    re.IGNORECASE,
)
_NOISE_TOKENS_RE = re.compile(
    r"[-_ ](?:x86_64|x86-64|amd64|x64|aarch64|arm64|armv7l?|i386|i686|linux|gnu|musl|portable|bin)\b",
    re.IGNORECASE,
)

# Helper binaries commonly bundled alongside Electron, Chromium, or JetBrains apps
_IGNORED_BIN_NAMES = frozenset(
    {
        "chrome-sandbox",
        "chrome_crashpad_handler",
        "crashpad_handler",
        "nacl_helper",
        "nacl_helper_bootstrap",
        "minidump-2-core",
        "xdg-open",
        "xdg-mime",
        "fsnotifier",
        "fsnotifier64",
        "restarter",
        "inspect.sh",
        "format.sh",
        "ltedit.sh",
        "profiler.sh",
        "game-tools.sh",
        "printenv.py",
        "restart.py",
        "language_server",
        "uninstall",
        "uninstall.sh",
    }
)
_IGNORED_BIN_SUFFIXES = (
    ".so",
    ".a",
    ".o",
    ".dll",
    ".exe",
    ".jar",
    ".pak",
    ".dat",
    ".bin",
    ".json",
    ".txt",
    ".md",
    ".html",
    ".png",
    ".svg",
    ".ico",
    ".xpm",
    ".desktop",
)


def is_archive_file(path: Path) -> bool:
    """Return True if path has a supported compressed archive suffix."""
    low = path.name.lower()
    return any(low.endswith(sfx) for sfx in ARCHIVE_SUFFIXES)


def strip_archive_suffix(filename: str) -> str:
    low = filename.lower()
    for sfx in ARCHIVE_SUFFIXES:
        if low.endswith(sfx):
            return filename[: -len(sfx)]
    return filename


def _guess_archive_name_and_version(filename: str) -> tuple[str, str]:
    stem = strip_archive_suffix(filename)
    version = ""
    ver_match = _VERSION_IN_NAME_RE.search(stem)
    if ver_match:
        version = ver_match.group(1)
        name_part = stem[: ver_match.start()]
    else:
        name_part = stem

    name_part = _NOISE_TOKENS_RE.sub("", name_part)
    name_clean = re.sub(r"[-_]+", " ", name_part).strip() or stem
    if name_clean == name_clean.lower():
        name_clean = name_clean.title()
    return name_clean, version


def _is_within_dir(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _unwrap_single_root(extract_dir: Path) -> Path:
    """If an extracted directory contains only a single subdirectory, descend into it."""
    current = extract_dir
    for _ in range(3):
        visible = [
            p
            for p in current.iterdir()
            if p.name not in ("__MACOSX", ".DS_Store")
        ]
        if len(visible) == 1 and visible[0].is_dir():
            current = visible[0]
        else:
            break
    return current


def _extract_archive_safely(archive_path: Path, dest_dir: Path) -> Path:
    """Extract tar.* or zip archive into dest_dir with path-traversal protection."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    low = archive_path.name.lower()

    if low.endswith(".zip"):
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                for info in zf.infolist():
                    member_path = dest_dir / info.filename
                    if not _is_within_dir(dest_dir, member_path):
                        raise FormatError(
                            f"Refusing to extract unsafe archive path: {info.filename!r}"
                        )
                zf.extractall(dest_dir)
                # Restore Unix executable permissions stored in ZIP external_attr
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    mode = (info.external_attr >> 16) & 0o777
                    if mode & 0o111:
                        extracted = dest_dir / info.filename
                        if extracted.is_file():
                            extracted.chmod(mode)
        except zipfile.BadZipFile as exc:
            raise FormatError(f"Invalid or corrupted ZIP archive: {archive_path.name}") from exc
        return _unwrap_single_root(dest_dir)

    # tar / tar.gz / tar.xz / tar.bz2
    try:
        with tarfile.open(archive_path, "r:*") as tf:
            members = tf.getmembers()
            for member in members:
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    raise FormatError(
                        f"Refusing to extract unsafe archive path: {member.name!r}"
                    )
                target = dest_dir / member.name
                if not _is_within_dir(dest_dir, target):
                    raise FormatError(
                        f"Refusing to extract unsafe archive path: {member.name!r}"
                    )
                if member.issym() or member.islnk():
                    link_dest = (target.parent / member.linkname).resolve()
                    if member.linkname.startswith("/") or not _is_within_dir(
                        dest_dir, link_dest
                    ):
                        raise FormatError(
                            f"Refusing to extract unsafe symlink: {member.name!r} -> {member.linkname!r}"
                        )
            try:
                tf.extractall(dest_dir, filter="data")
            except TypeError:
                tf.extractall(dest_dir)
    except (tarfile.TarError, OSError) as exc:
        raise FormatError(
            f"Invalid or corrupted tar archive {archive_path.name!r}: {exc}"
        ) from exc

    return _unwrap_single_root(dest_dir)


def _is_likely_executable(path: Path) -> bool:
    """Return True if path is a runnable binary or shell script (not a shared lib/helper)."""
    if not path.is_file() or path.is_symlink():
        return False
    low = path.name.lower()
    if low in _IGNORED_BIN_NAMES:
        return False
    if any(low.endswith(sfx) for sfx in _IGNORED_BIN_SUFFIXES):
        return False
    if ".so." in low:
        return False

    try:
        st = path.stat()
    except OSError:
        return False
    is_exec_bit = bool(st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    if not is_exec_bit and not low.endswith(".sh"):
        return False

    try:
        with path.open("rb") as fh:
            head = fh.read(4)
    except OSError:
        return False
    return head == _ELF_MAGIC or head.startswith(b"#!")


def _detect_icon_extension(data: bytes, suffix: str) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    head = data[:256].lstrip()
    if head.startswith(b"<svg") or (
        head.startswith(b"<?xml") and b"<svg" in data[:1024]
    ):
        return "svg"
    if suffix.lower() in (".png", ".svg", ".xpm", ".ico", ".webp"):
        return suffix.lower().lstrip(".")
    return ""


def inspect_portable_directory(
    root_dir: Path,
    fallback_filename: str = "",
) -> ExtractedMetadata:
    """Inspect an extracted portable application directory for name, launcher, version, and icon."""
    content_root = _unwrap_single_root(root_dir)
    fallback_name, fallback_version = _guess_archive_name_and_version(
        fallback_filename or content_root.name
    )

    name = ""
    version = ""
    comment = ""
    categories = "Utility;"
    desktop_text = ""
    wm_class = ""
    exec_relpath = ""
    icon_file: Path | None = None

    # 1. JetBrains / Android Studio `product-info.json`
    product_info = content_root / "product-info.json"
    if product_info.is_file():
        try:
            data = json.loads(product_info.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                name = str(data.get("name") or "").strip()
                version = str(data.get("version") or "").strip()
                categories = "Development;IDE;"
                svg_rel = str(data.get("svgIconPath") or "").strip()
                if svg_rel and (content_root / svg_rel).is_file():
                    icon_file = content_root / svg_rel
                launches = data.get("launch")
                if isinstance(launches, list) and launches and isinstance(launches[0], dict):
                    lp = str(launches[0].get("launcherPath") or "").strip()
                    if lp and (content_root / lp).is_file():
                        exec_relpath = lp
        except (OSError, json.JSONDecodeError):
            pass

    # 2. Embedded `.desktop` file (top-level or share/applications)
    desktop_candidates = list(content_root.glob("*.desktop")) + list(
        (content_root / "share" / "applications").glob("*.desktop")
        if (content_root / "share" / "applications").is_dir()
        else []
    )
    if desktop_candidates:
        try:
            desktop_text = desktop_candidates[0].read_text(
                encoding="utf-8", errors="replace"
            )
            parsed = parse_desktop_metadata(desktop_text)
            if not name and parsed.get("name"):
                name = parsed["name"]
            if not comment and parsed.get("comment"):
                comment = parsed["comment"]
            if parsed.get("categories"):
                categories = parsed["categories"]
            if parsed.get("wm_class"):
                wm_class = parsed["wm_class"]
            if not version and parsed.get("version"):
                version = parsed["version"]
        except OSError:
            pass

    # 3. Electron / generic `version` or `resources/app/package.json`
    if not version:
        ver_file = content_root / "version"
        if ver_file.is_file() and ver_file.stat().st_size < 128:
            raw_ver = ver_file.read_text(encoding="utf-8", errors="replace").strip()
            if raw_ver and len(raw_ver.splitlines()) == 1:
                version = raw_ver
    pkg_json = content_root / "resources" / "app" / "package.json"
    if pkg_json.is_file():
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
            if isinstance(pkg, dict):
                if not name:
                    name = str(pkg.get("productName") or pkg.get("name") or "").strip()
                if not version and pkg.get("version"):
                    version = str(pkg["version"]).strip()
                if not comment and pkg.get("description"):
                    comment = str(pkg["description"]).strip()
        except (OSError, json.JSONDecodeError):
            pass

    # 4. Auto-detect main executable if not already declared
    if not exec_relpath:
        exec_file = _find_best_executable(content_root, name or fallback_name)
        if exec_file:
            exec_relpath = str(exec_file.relative_to(root_dir))
    else:
        # Express exec_relpath relative to root_dir (in case content_root was unwrapped)
        exec_relpath = str((content_root / exec_relpath).relative_to(root_dir))

    # 5. Auto-detect icon if not already found
    if icon_file is None:
        icon_file = _find_best_icon(
            content_root,
            name_hint=name or fallback_name,
            exec_stem=Path(exec_relpath).stem if exec_relpath else "",
        )

    icon_bytes: bytes | None = None
    icon_ext = ""
    if icon_file and icon_file.is_file():
        try:
            raw = icon_file.read_bytes()
            ext = _detect_icon_extension(raw, icon_file.suffix)
            if raw and ext:
                icon_bytes = raw
                icon_ext = ext
        except OSError:
            pass

    # 6. Electron fallback: inspect resources/app.asar for an embedded app icon
    if not icon_bytes:
        asar_bytes, asar_ext = _extract_icon_from_electron_asar(
            content_root,
            name_hint=name or fallback_name,
            exec_stem=Path(exec_relpath).stem if exec_relpath else "",
        )
        if asar_bytes and asar_ext:
            icon_bytes = asar_bytes
            icon_ext = asar_ext

    return ExtractedMetadata(
        name=name or fallback_name,
        comment=comment,
        categories=categories or "Utility;",
        icon_bytes=icon_bytes,
        icon_ext=icon_ext,
        desktop_content=desktop_text,
        wm_class=wm_class,
        version_hint=version or fallback_version,
        exec_relpath=exec_relpath,
    )


def _find_best_executable(content_root: Path, name_hint: str) -> Path | None:
    """Heuristically rank runnable binaries/scripts inside a portable app directory."""
    norm_hint = re.sub(r"[^a-z0-9]+", "", name_hint.lower())
    candidates: list[tuple[int, int, Path]] = []

    search_dirs = [content_root]
    bin_dir = content_root / "bin"
    if bin_dir.is_dir():
        search_dirs.append(bin_dir)

    for depth, directory in enumerate(search_dirs):
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not _is_likely_executable(entry):
                continue
            stem_norm = re.sub(r"[^a-z0-9]+", "", entry.stem.lower())
            score = 0
            # Strong match if executable name matches app name
            if norm_hint and (stem_norm == norm_hint or stem_norm in norm_hint or norm_hint in stem_norm):
                score += 100
            # Prefer top-level binaries (Electron/Firefox/Blender) and bin/ launchers (JetBrains/Android Studio)
            if depth == 0:
                score += 30
            else:
                score += 25
            # Prefer native ELF over .sh wrapper slightly when both exist (e.g. bin/studio vs bin/studio.sh)
            if not entry.name.lower().endswith(".sh"):
                score += 10
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            candidates.append((score, size, entry))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], -item[1], item[2].name))
    return candidates[0][2]


def _find_best_icon(
    content_root: Path,
    name_hint: str = "",
    exec_stem: str = "",
) -> Path | None:
    """Search common locations inside a portable app folder for its best icon."""
    icon_exts = (".svg", ".png", ".ico", ".xpm")
    hints = {
        re.sub(r"[^a-z0-9]+", "", h.lower())
        for h in (name_hint, exec_stem)
        if h
    }

    search_dirs = [
        content_root,
        content_root / "bin",
        content_root / "resources",
        content_root / "resources" / "app" / "resources" / "linux",
        content_root / "resources" / "app" / "resources",
        content_root / "share" / "pixmaps",
        content_root / "share" / "icons" / "hicolor" / "scalable" / "apps",
        content_root / "share" / "icons" / "hicolor" / "512x512" / "apps",
        content_root / "share" / "icons" / "hicolor" / "256x256" / "apps",
        content_root / "share" / "icons" / "hicolor" / "128x128" / "apps",
    ]

    candidates: list[tuple[int, int, Path]] = []
    for idx, d in enumerate(search_dirs):
        if not d.is_dir():
            continue
        try:
            files = sorted(d.iterdir())
        except OSError:
            continue
        for f in files:
            if not f.is_file():
                continue
            low = f.name.lower()
            if not any(low.endswith(ext) for ext in icon_exts):
                continue
            stem_norm = re.sub(r"[^a-z0-9]+", "", f.stem.lower())
            score = 50 - idx * 3
            if any(h and (stem_norm == h or h in stem_norm) for h in hints):
                score += 80
            if "icon" in low or "logo" in low:
                score += 20
            if low.endswith(".svg"):
                score += 15
            elif low.endswith(".png"):
                score += 10
            try:
                size = f.stat().st_size
            except OSError:
                size = 0
            candidates.append((score, size, f))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], -item[1], item[2].name))
    return candidates[0][2]


def _extract_icon_from_electron_asar(
    content_root: Path,
    name_hint: str = "",
    exec_stem: str = "",
) -> tuple[bytes | None, str]:
    """Read an app icon from Electron's resources/app.asar using pure stdlib."""
    asar_path = content_root / "resources" / "app.asar"
    if not asar_path.is_file():
        return None, ""

    icon_exts = (".svg", ".png", ".ico")
    hints = {
        re.sub(r"[^a-z0-9]+", "", h.lower())
        for h in (name_hint, exec_stem)
        if h
    }

    try:
        file_size = asar_path.stat().st_size
        with asar_path.open("rb") as fh:
            hdr = fh.read(16)
            if len(hdr) < 16:
                return None, ""
            header_size = struct.unpack("<I", hdr[4:8])[0]
            str_len = struct.unpack("<I", hdr[12:16])[0]
            data_base = 8 + header_size
            if str_len <= 0 or str_len > 32 * 1024 * 1024 or data_base >= file_size:
                return None, ""
            meta = json.loads(fh.read(str_len).decode("utf-8"))
            if not isinstance(meta, dict):
                return None, ""

            candidates: list[tuple[int, int, str, dict[str, object]]] = []

            def walk(node: dict[str, object], prefix: str = "") -> None:
                files = node.get("files")
                if not isinstance(files, dict):
                    return
                for fname, entry in files.items():
                    if not isinstance(entry, dict):
                        continue
                    rel = f"{prefix}/{fname}" if prefix else fname
                    low_rel = rel.lower()
                    if "files" in entry:
                        # Skip third-party npm dependencies to avoid picking library icons
                        if fname.lower() == "node_modules":
                            continue
                        walk(entry, rel)
                        continue
                    low_name = fname.lower()
                    if not any(low_name.endswith(ext) for ext in icon_exts):
                        continue
                    size_val = entry.get("size", 0)
                    if not isinstance(size_val, int) or size_val <= 0 or size_val > 10 * 1024 * 1024:
                        continue

                    depth = rel.count("/")
                    stem_norm = re.sub(r"[^a-z0-9]+", "", Path(fname).stem.lower())
                    score = max(0, 40 - depth * 8)
                    if stem_norm in ("icon", "appicon", "logo"):
                        score += 70
                    if any(h and (stem_norm == h or h in stem_norm) for h in hints):
                        score += 80
                    if "icon" in low_rel or "logo" in low_rel:
                        score += 20
                    if "tray" in low_rel or "template" in low_name or "badge" in low_name:
                        score -= 40
                    if low_name.endswith(".svg"):
                        score += 12
                    elif low_name.endswith(".png"):
                        score += 10
                    candidates.append((score, size_val, rel, entry))

            walk(meta)
            if not candidates:
                return None, ""

            candidates.sort(key=lambda item: (-item[0], -item[1], item[2]))
            for _, size_val, rel, entry in candidates[:5]:
                raw: bytes | None = None
                if entry.get("unpacked"):
                    unpacked = content_root / "resources" / "app.asar.unpacked" / rel
                    if unpacked.is_file():
                        raw = unpacked.read_bytes()
                else:
                    off_str = entry.get("offset")
                    if off_str is None:
                        continue
                    offset = int(str(off_str))
                    abs_off = data_base + offset
                    if abs_off < data_base or abs_off + size_val > file_size:
                        continue
                    fh.seek(abs_off)
                    raw = fh.read(size_val)

                if raw:
                    ext = _detect_icon_extension(raw, Path(rel).suffix)
                    if ext:
                        return raw, ext
    except (OSError, ValueError, struct.error, json.JSONDecodeError):
        return None, ""

    return None, ""


class ArchiveFormat(PackageFormat):
    """Handles portable Linux archives (.tar.gz, .tar.xz, .tar.bz2, .zip) and directories."""

    format_id = "archive"

    def can_handle(self, path: Path) -> bool:
        if path.is_dir():
            return self.looks_like_portable_app_dir(path)
        return is_archive_file(path)

    @staticmethod
    def looks_like_portable_app_dir(directory: Path) -> bool:
        """Return True if an existing directory inside ~/Apps looks like a portable app."""
        if not directory.is_dir() or directory.name.startswith("."):
            return False
        root = _unwrap_single_root(directory)
        if (root / "product-info.json").is_file():
            return True
        if list(root.glob("*.desktop")):
            return True
        return _find_best_executable(root, root.name) is not None

    def inspect(self, path: Path) -> ExtractedMetadata:
        """Safely inspect an archive file or an already-extracted app directory."""
        if path.is_dir():
            return inspect_portable_directory(path)

        if not path.is_file() or not is_archive_file(path):
            raise FormatError(f"Unsupported archive file: {path.name}")

        with tempfile.TemporaryDirectory(prefix="appima-archive-inspect-") as tmp_str:
            extracted_root = _extract_archive_safely(path, Path(tmp_str))
            return inspect_portable_directory(
                extracted_root, fallback_filename=path.name
            )

    def install_file(
        self,
        staged_path: Path,
        apps_dir: Path,
        app_id: str,
        in_place: bool = False,
    ) -> Path:
        """Extract an archive into apps_dir/<app_id>/ (or adopt a directory in place)."""
        apps_dir.mkdir(parents=True, exist_ok=True)

        if staged_path.is_dir():
            if in_place:
                return staged_path
            dest_dir = apps_dir / app_id
            tmp_dir = apps_dir / f".{app_id}.installing"
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir)
            shutil.copytree(staged_path, tmp_dir, symlinks=True)
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            tmp_dir.replace(dest_dir)
            return dest_dir

        if not is_archive_file(staged_path):
            raise FormatError(f"Unsupported archive format: {staged_path.name}")

        dest_dir = apps_dir / app_id
        tmp_extract = apps_dir / f".{app_id}.extracting"
        if tmp_extract.exists():
            shutil.rmtree(tmp_extract)

        try:
            unwrapped = _extract_archive_safely(staged_path, tmp_extract)
            final_stage = apps_dir / f".{app_id}.ready"
            if final_stage.exists():
                shutil.rmtree(final_stage)
            if unwrapped != tmp_extract:
                shutil.move(str(unwrapped), str(final_stage))
                shutil.rmtree(tmp_extract, ignore_errors=True)
            else:
                tmp_extract.replace(final_stage)

            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            final_stage.replace(dest_dir)
            return dest_dir
        except Exception:
            shutil.rmtree(tmp_extract, ignore_errors=True)
            shutil.rmtree(apps_dir / f".{app_id}.ready", ignore_errors=True)
            raise

    def remove_installed(self, install_path: Path) -> None:
        if install_path.is_dir():
            shutil.rmtree(install_path)
        else:
            install_path.unlink(missing_ok=True)
