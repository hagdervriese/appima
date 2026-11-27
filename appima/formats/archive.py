"""Foundation stub for portable archive bundles (.tar.gz, .tar.xz, .zip).

Planned for v0.2 to manage self-contained directory bundles such as
Android Studio, JetBrains IDEs, and Antigravity alongside AppImages.
"""

from __future__ import annotations

from pathlib import Path

from appima.core.models import ExtractedMetadata, FormatError
from appima.formats.base import PackageFormat

_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".zip")


class ArchiveFormat(PackageFormat):
    """Placeholder format handler reserved for portable archive apps (v0.2)."""

    format_id = "archive"

    def can_handle(self, path: Path) -> bool:
        lower = path.name.lower()
        return any(lower.endswith(sfx) for sfx in _ARCHIVE_SUFFIXES)

    def inspect(self, path: Path) -> ExtractedMetadata:
        raise FormatError(
            "Portable archive bundles (.tar.gz / .zip) are planned for appima v0.2."
        )

    def install_file(
        self,
        staged_path: Path,
        apps_dir: Path,
        app_id: str,
        in_place: bool = False,
    ) -> Path:
        raise FormatError(
            "Portable archive bundles (.tar.gz / .zip) are planned for appima v0.2."
        )

    def remove_installed(self, install_path: Path) -> None:
        raise FormatError(
            "Portable archive bundles (.tar.gz / .zip) are planned for appima v0.2."
        )
