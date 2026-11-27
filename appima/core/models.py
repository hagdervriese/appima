"""Domain models and exception types for appima."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class AppimaError(Exception):
    """Base exception for all appima errors."""


class ChecksumMismatchError(AppimaError):
    """Raised when a downloaded or local file fails checksum verification."""

    def __init__(self, expected: str, actual: str, algorithm: str) -> None:
        self.expected = expected
        self.actual = actual
        self.algorithm = algorithm
        super().__init__(
            f"{algorithm.upper()} checksum mismatch: expected {expected}, got {actual}"
        )


class ProviderError(AppimaError):
    """Raised when a source provider cannot resolve or fetch release data."""


class FormatError(AppimaError):
    """Raised when a package file is invalid or cannot be processed."""


class IntegrationError(AppimaError):
    """Raised when XDG desktop integration fails."""


VALID_CHECK_INTERVALS = ("off", "6h", "daily", "weekly")


@dataclass
class Config:
    """User configuration persisted in config.json."""

    apps_dir: Path = field(default_factory=lambda: Path.home() / "Apps")
    check_interval: str = "daily"
    keep_backup: bool = True
    github_token: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "apps_dir": str(self.apps_dir),
            "check_interval": self.check_interval,
            "keep_backup": self.keep_backup,
            "github_token": self.github_token,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Config":
        raw_dir = str(data.get("apps_dir") or (Path.home() / "Apps"))
        interval = str(data.get("check_interval") or "daily")
        if interval not in VALID_CHECK_INTERVALS:
            interval = "daily"
        return cls(
            apps_dir=Path(raw_dir).expanduser(),
            check_interval=interval,
            keep_backup=bool(data.get("keep_backup", True)),
            github_token=str(data.get("github_token") or ""),
        )


@dataclass
class ReleaseInfo:
    """Information about a downloadable or local release."""

    provider: str
    version: str
    download_url: str
    filename: str
    name_hint: str = ""
    checksum: str = ""
    checksum_algo: str = ""
    checksum_url: str = ""
    release_url: str = ""
    release_notes: str = ""
    etag: str = ""
    last_modified: str = ""
    size_bytes: int | None = None
    asset_pattern: str = ""


@dataclass
class ExtractedMetadata:
    """Desktop metadata extracted from a package (AppImage or archive)."""

    name: str
    comment: str = ""
    categories: str = "Utility;"
    icon_bytes: bytes | None = None
    icon_ext: str = ""
    desktop_content: str = ""
    mime_types: str = ""
    wm_class: str = ""
    terminal: bool = False
    version_hint: str = ""


@dataclass
class AppRecord:
    """Persistent state of a managed application."""

    id: str
    name: str
    format: str  # "appimage" (future: "archive")
    provider: str  # "github", "direct_url", "local"
    source_url: str
    installed_version: str
    install_path: str
    asset_pattern: str = ""
    available_version: str = ""
    available_download_url: str = ""
    available_checksum: str = ""
    available_checksum_algo: str = ""
    backup_path: str = ""
    backup_version: str = ""
    desktop_file: str = ""
    icon_path: str = ""
    checksum_algo: str = ""
    checksum_value: str = ""
    checksum_verified: bool = False
    etag: str = ""
    last_modified: str = ""
    launch_args: str = ""
    env_vars: str = ""
    release_url: str = ""
    release_notes: str = ""
    installed_at: str = ""
    updated_at: str = ""
    last_checked_at: str = ""

    @property
    def has_update(self) -> bool:
        return bool(
            self.available_version
            and self.available_version != self.installed_version
        )

    @property
    def has_backup(self) -> bool:
        return bool(self.backup_path and Path(self.backup_path).exists())

    @property
    def is_present(self) -> bool:
        return bool(self.install_path and Path(self.install_path).exists())


@dataclass
class UpdateCheckResult:
    """Outcome of checking a single managed app for updates."""

    app_id: str
    app_name: str
    installed_version: str
    available_version: str = ""
    update_available: bool = False
    error: str = ""


@dataclass
class DesktopConflict:
    """An existing desktop launcher entry matching or closely resembling an app name."""

    name: str
    desktop_path: str
    exec_cmd: str = ""
    source_kind: str = "System"
    similarity: float = 1.0
    is_appima_managed: bool = False
