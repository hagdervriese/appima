"""Core engine for appima (pure Python standard library)."""

from appima.core.models import (
    AppimaError,
    AppRecord,
    ChecksumMismatchError,
    Config,
    DesktopConflict,
    ExtractedMetadata,
    FormatError,
    IntegrationError,
    ProviderError,
    ReleaseInfo,
    RemovalPlan,
    UnmanagedPreview,
    UpdateCheckResult,
)

__all__ = [
    "AppimaError",
    "AppRecord",
    "ChecksumMismatchError",
    "Config",
    "DesktopConflict",
    "ExtractedMetadata",
    "FormatError",
    "IntegrationError",
    "ProviderError",
    "ReleaseInfo",
    "RemovalPlan",
    "UnmanagedPreview",
    "UpdateCheckResult",
]
