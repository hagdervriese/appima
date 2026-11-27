"""Safe AppImage format handler (SquashFS inspection via unsquashfs)."""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

from appima.core.desktop import parse_desktop_metadata
from appima.core.models import ExtractedMetadata, FormatError
from appima.core.runtime import current_appdir
from appima.formats.base import PackageFormat

_ELF_MAGIC = b"\x7fELF"
_SQUASHFS_MAGIC = b"hsqs"
_VERSION_IN_NAME_RE = re.compile(
    r"[-_ ]v?(\d+\.\d+(?:\.\d+)?(?:[-.](?:alpha|beta|rc|pre|dev|nightly|post)[0-9A-Za-z.]*)?)",
    re.IGNORECASE,
)
_ARCH_TOKENS_RE = re.compile(
    r"[-_ ](?:x86_64|x86-64|amd64|x64|aarch64|arm64|armv7l?|i386|i686|riscv64|linux|gnu|musl|appimage)\b",
    re.IGNORECASE,
)


def _guess_name_and_version(filename: str) -> tuple[str, str]:
    """Derive a human-readable app name and optional version from a filename."""
    stem = filename
    if stem.lower().endswith(".appimage"):
        stem = stem[: -len(".appimage")]

    version = ""
    ver_match = _VERSION_IN_NAME_RE.search(stem)
    if ver_match:
        version = ver_match.group(1)
        name_part = stem[: ver_match.start()]
    else:
        name_part = stem

    name_part = _ARCH_TOKENS_RE.sub("", name_part)
    name_clean = re.sub(r"[-_]+", " ", name_part).strip()
    if not name_clean:
        name_clean = stem
    # Title-case only if all lowercase
    if name_clean == name_clean.lower():
        name_clean = name_clean.title()
    return name_clean, version


def find_squashfs_offset(path: Path) -> int | None:
    """Locate the embedded SquashFS offset inside a Type 2 AppImage without running it."""
    try:
        file_size = path.stat().st_size
        with path.open("rb") as fh:
            header = fh.read(64)
            if len(header) < 52 or header[:4] != _ELF_MAGIC:
                return None

            ei_class = header[4]  # 1 = 32-bit, 2 = 64-bit
            ei_data = header[5]   # 1 = little-endian, 2 = big-endian
            endian = "<" if ei_data == 1 else ">"

            elf_end: int | None = None
            if ei_class == 2 and len(header) >= 64:
                e_shoff = struct.unpack(f"{endian}Q", header[40:48])[0]
                e_shentsize = struct.unpack(f"{endian}H", header[58:60])[0]
                e_shnum = struct.unpack(f"{endian}H", header[60:62])[0]
                if e_shoff and e_shentsize and e_shnum:
                    elf_end = e_shoff + e_shentsize * e_shnum
            elif ei_class == 1 and len(header) >= 52:
                e_shoff = struct.unpack(f"{endian}I", header[32:36])[0]
                e_shentsize = struct.unpack(f"{endian}H", header[46:48])[0]
                e_shnum = struct.unpack(f"{endian}H", header[48:50])[0]
                if e_shoff and e_shentsize and e_shnum:
                    elf_end = e_shoff + e_shentsize * e_shnum

            if elf_end and 0 < elf_end < file_size:
                fh.seek(elf_end)
                if fh.read(4) == _SQUASHFS_MAGIC:
                    return elf_end

            # Fallback: scan the first 2 MiB for the SquashFS superblock magic
            fh.seek(0)
            probe = fh.read(min(file_size, 2 * 1024 * 1024))
            idx = probe.find(_SQUASHFS_MAGIC)
            if idx != -1:
                return idx
    except OSError:
        return None
    return None


class AppImageFormat(PackageFormat):
    """Handles single-file Linux AppImages."""

    format_id = "appimage"

    def can_handle(self, path: Path) -> bool:
        if path.suffix.lower() == ".appimage":
            return True
        try:
            with path.open("rb") as fh:
                head = fh.read(16)
            return head[:4] == _ELF_MAGIC and b"AI\x02" in head
        except OSError:
            return False

    def validate(self, path: Path) -> None:
        """Ensure file looks like a valid ELF binary / AppImage."""
        if not path.is_file():
            raise FormatError(f"AppImage file not found: {path}")
        if path.stat().st_size < 64:
            raise FormatError(f"File is too small to be a valid AppImage: {path.name}")
        with path.open("rb") as fh:
            magic = fh.read(4)
        if magic != _ELF_MAGIC:
            raise FormatError(
                f"File {path.name!r} is not a valid ELF AppImage binary."
            )

    @staticmethod
    def _find_unsquashfs() -> str | None:
        found = shutil.which("unsquashfs")
        if found:
            return found
        appdir = current_appdir()
        if appdir:
            bundled = appdir / "usr" / "bin" / "unsquashfs"
            if bundled.is_file():
                return str(bundled)
        return None

    def inspect(self, path: Path) -> ExtractedMetadata:
        """Inspect .desktop and icon from the AppImage SquashFS safely."""
        self.validate(path)
        fallback_name, fallback_version = _guess_name_and_version(path.name)

        unsquashfs = self._find_unsquashfs()
        offset = find_squashfs_offset(path)
        if not unsquashfs or offset is None:
            return ExtractedMetadata(
                name=fallback_name,
                version_hint=fallback_version,
            )

        try:
            return self._inspect_squashfs(
                path, offset, unsquashfs, fallback_name, fallback_version
            )
        except (OSError, subprocess.SubprocessError):
            return ExtractedMetadata(
                name=fallback_name,
                version_hint=fallback_version,
            )

    def _inspect_squashfs(
        self,
        path: Path,
        offset: int,
        unsquashfs: str,
        fallback_name: str,
        fallback_version: str,
    ) -> ExtractedMetadata:
        list_proc = subprocess.run(
            [unsquashfs, "-o", str(offset), "-l", str(path)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if list_proc.returncode != 0:
            return ExtractedMetadata(name=fallback_name, version_hint=fallback_version)

        entries: list[str] = []
        for raw_line in list_proc.stdout.splitlines():
            line = raw_line.strip()
            if line.startswith("squashfs-root/"):
                entries.append(line[len("squashfs-root/") :])

        desktop_candidates = [
            e for e in entries if "/" not in e and e.endswith(".desktop")
        ]
        if not desktop_candidates:
            desktop_candidates = [
                e
                for e in entries
                if e.startswith("usr/share/applications/") and e.endswith(".desktop")
            ]

        with tempfile.TemporaryDirectory(prefix="appima-inspect-") as tmp_str:
            tmp_dir = Path(tmp_str)
            extract_root = tmp_dir / "root"

            # Step 1: Extract the .desktop file first so we can read its Icon= hint
            desktop_text = ""
            if desktop_candidates:
                subprocess.run(
                    [
                        unsquashfs,
                        "-o",
                        str(offset),
                        "-f",
                        "-n",
                        "-q",
                        "-d",
                        str(extract_root),
                        str(path),
                        desktop_candidates[0],
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=20,
                    check=False,
                )
                df = extract_root / desktop_candidates[0]
                if df.is_file():
                    desktop_text = df.read_text(encoding="utf-8", errors="replace")

            parsed = parse_desktop_metadata(desktop_text) if desktop_text else {}
            icon_hint = parsed.get("icon", "")

            # Step 2: Rank icon candidates using the .desktop Icon= hint + standard AppImage paths
            icon_candidates = self._rank_icon_candidates(entries, icon_hint)

            icon_bytes: bytes | None = None
            icon_ext = ""
            if icon_candidates:
                batch = icon_candidates[:8]
                subprocess.run(
                    [
                        unsquashfs,
                        "-o",
                        str(offset),
                        "-f",
                        "-n",
                        "-q",
                        "-d",
                        str(extract_root),
                        str(path),
                        *batch,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=20,
                    check=False,
                )
                for cand in batch:
                    cand_path = extract_root / cand
                    resolved_bytes, resolved_ext = self._read_extracted_icon(
                        cand_path, extract_root, path, offset, unsquashfs
                    )
                    if resolved_bytes:
                        icon_bytes = resolved_bytes
                        icon_ext = resolved_ext
                        break

        return ExtractedMetadata(
            name=parsed.get("name") or fallback_name,
            comment=parsed.get("comment", ""),
            categories=parsed.get("categories") or "Utility;",
            icon_bytes=icon_bytes,
            icon_ext=icon_ext,
            desktop_content=desktop_text,
            mime_types=parsed.get("mime_types", ""),
            wm_class=parsed.get("wm_class", ""),
            terminal=parsed.get("terminal") == "true",
            version_hint=parsed.get("version") or fallback_version,
        )

    @staticmethod
    def _rank_icon_candidates(entries: list[str], icon_hint: str) -> list[str]:
        """Return ordered icon paths inside SquashFS from best to fallback."""
        icon_exts = (".svg", ".png", ".xpm", ".ico")
        hint_stem = Path(icon_hint).stem.lower() if icon_hint else ""
        size_order = (
            "scalable",
            "1024x1024",
            "512x512",
            "256x256",
            "128x128",
            "0x0",
            "96x96",
            "64x64",
            "48x48",
        )

        ranked: list[str] = []

        def add_unique(items: list[str]) -> None:
            for item in items:
                if item not in ranked:
                    ranked.append(item)

        # 1. Exact matches for the .desktop Icon= name (highest resolution / SVG first)
        if hint_stem:
            for size_dir in size_order:
                for ext in (".svg", ".png"):
                    add_unique(
                        [
                            e
                            for e in entries
                            if f"/hicolor/{size_dir}/apps/" in f"/{e}"
                            and Path(e).stem.lower() == hint_stem
                            and e.lower().endswith(ext)
                        ]
                    )
            # Top-level or pixmaps match for Icon= name
            for ext in icon_exts:
                add_unique(
                    [
                        e
                        for e in entries
                        if "/" not in e
                        and Path(e).stem.lower() == hint_stem
                        and e.lower().endswith(ext)
                    ]
                )
                add_unique(
                    [
                        e
                        for e in entries
                        if e.startswith("usr/share/pixmaps/")
                        and Path(e).stem.lower() == hint_stem
                        and e.lower().endswith(ext)
                    ]
                )
                # Any other icon path matching the hint stem
                add_unique(
                    [
                        e
                        for e in entries
                        if Path(e).stem.lower() == hint_stem
                        and e.lower().endswith(ext)
                    ]
                )

        # 2. Standard AppImage .DirIcon
        if ".DirIcon" in entries:
            add_unique([".DirIcon"])

        # 3. Top-level icons
        for ext in icon_exts:
            add_unique([e for e in entries if "/" not in e and e.lower().endswith(ext)])

        # 4. Any hicolor apps icons by descending size preference
        for size_dir in size_order:
            for ext in (".svg", ".png"):
                add_unique(
                    [
                        e
                        for e in entries
                        if e.startswith(f"usr/share/icons/hicolor/{size_dir}/apps/")
                        and e.lower().endswith(ext)
                    ]
                )

        # 5. Any pixmaps icon
        for ext in icon_exts:
            add_unique(
                [
                    e
                    for e in entries
                    if e.startswith("usr/share/pixmaps/") and e.lower().endswith(ext)
                ]
            )

        return ranked

    def _read_extracted_icon(
        self,
        cand_path: Path,
        extract_root: Path,
        appimage_path: Path,
        offset: int,
        unsquashfs: str,
    ) -> tuple[bytes | None, str]:
        """Read an icon file, following multi-hop symlinks inside the SquashFS."""
        root_resolved = extract_root.resolve()
        current = cand_path
        for _ in range(6):
            if not current.is_symlink():
                break
            link_target = os.readlink(current)
            if link_target.startswith("/"):
                inner_rel = link_target.lstrip("/")
            else:
                # Resolve relative to current's directory inside extract_root
                combined = os.path.normpath(
                    os.path.join(str(current.parent), link_target)
                )
                try:
                    inner_rel = str(Path(combined).relative_to(root_resolved))
                except ValueError:
                    inner_rel = link_target.lstrip("./")

            inner_rel = inner_rel.lstrip("./")
            target_path = extract_root / inner_rel
            if not target_path.exists() and not target_path.is_symlink():
                subprocess.run(
                    [
                        unsquashfs,
                        "-o",
                        str(offset),
                        "-f",
                        "-n",
                        "-q",
                        "-d",
                        str(extract_root),
                        str(appimage_path),
                        inner_rel,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
            current = target_path

        if not current.is_file():
            return None, ""

        data = current.read_bytes()
        if not data:
            return None, ""

        ext = self._detect_icon_extension(data, current.suffix)
        return (data, ext) if ext else (None, "")

    @staticmethod
    def _detect_icon_extension(data: bytes, suffix: str) -> str:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        head = data[:256].lstrip()
        if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:1024]):
            return "svg"
        if suffix.lower() in (".png", ".svg", ".xpm", ".ico"):
            return suffix.lower().lstrip(".")
        return ""

    def install_file(
        self,
        staged_path: Path,
        apps_dir: Path,
        app_id: str,
        in_place: bool = False,
    ) -> Path:
        """Install the AppImage into apps_dir with a stable filename and +x permissions."""
        self.validate(staged_path)
        apps_dir.mkdir(parents=True, exist_ok=True)

        if in_place:
            staged_path.chmod(0o755)
            return staged_path

        dest_path = apps_dir / f"{app_id}.AppImage"
        tmp_dest = apps_dir / f".{app_id}.AppImage.tmp"
        shutil.copy2(staged_path, tmp_dest)
        tmp_dest.chmod(0o755)
        tmp_dest.replace(dest_path)
        return dest_path

    def remove_installed(self, install_path: Path) -> None:
        install_path.unlink(missing_ok=True)
