"""Abstract base class for portable package formats."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from appima.core.models import ExtractedMetadata


class PackageFormat(ABC):
    """Interface implemented by each supported portable format (AppImage, Archive, ...)."""

    format_id: str = ""

    @abstractmethod
    def can_handle(self, path: Path) -> bool:
        """Return True if this handler can process the given file."""

    @abstractmethod
    def inspect(self, path: Path) -> ExtractedMetadata:
        """Safely inspect package metadata without executing untrusted code."""

    @abstractmethod
    def install_file(
        self,
        staged_path: Path,
        apps_dir: Path,
        app_id: str,
        in_place: bool = False,
    ) -> Path:
        """Place the package into apps_dir and return the installed path."""

    @abstractmethod
    def remove_installed(self, install_path: Path) -> None:
        """Remove an installed package from disk."""
