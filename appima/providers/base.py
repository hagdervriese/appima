"""Base provider interface and shared version/slug helpers."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

from appima.core.models import AppRecord, ReleaseInfo

_VERSION_RE = re.compile(
    r"v?(\d+\.\d+(?:\.\d+)?(?:[-.](?:alpha|beta|rc|pre|dev|nightly|post)[0-9A-Za-z.]*)?)",
    re.IGNORECASE,
)
_ARCH_OS_STRIP_RE = re.compile(
    r"[-_ ](?:x86_64|x86-64|amd64|x64|aarch64|arm64|armv7l?|i386|i686|riscv64|linux|gnu|musl|appimage)\b",
    re.IGNORECASE,
)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify_app_id(name: str) -> str:
    """Convert an application name into a clean filesystem-safe slug."""
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or "app"


def normalize_version(version: str) -> str:
    """Strip common leading prefixes like 'v' or 'release-' for display & comparison."""
    cleaned = version.strip()
    for prefix in ("release-", "rel-", "v"):
        if cleaned.lower().startswith(prefix) and len(cleaned) > len(prefix):
            rest = cleaned[len(prefix) :]
            if rest[0].isdigit():
                return rest
    return cleaned


def _version_sort_key(version: str) -> tuple[tuple[int, int | str], ...]:
    """Convert a version string into a comparable tuple (numeric-aware)."""
    norm = normalize_version(version)
    tokens = re.findall(r"\d+|[A-Za-z]+", norm)
    parts: list[tuple[int, int | str]] = []
    for tok in tokens:
        if tok.isdigit():
            parts.append((1, int(tok)))
        else:
            parts.append((0, tok.lower()))
    return tuple(parts)


def is_newer_version(installed: str, candidate: str) -> bool:
    """Return True if candidate represents a newer release than installed."""
    norm_inst = normalize_version(installed)
    norm_cand = normalize_version(candidate)
    if not norm_cand or norm_cand == norm_inst:
        return False
    if not norm_inst or norm_inst == "unknown":
        return True

    # If both look like numeric versions, compare numerically
    if norm_inst[0].isdigit() and norm_cand[0].isdigit():
        return _version_sort_key(norm_cand) > _version_sort_key(norm_inst)

    # For ETag / date-based fingerprints, any difference indicates an update
    return norm_cand != norm_inst


def extract_version_from_filename(filename: str) -> str:
    """Extract a version token from a release filename, or empty string if none."""
    stem = filename
    low = stem.lower()
    for suffix in (
        ".appimage",
        ".tar.gz",
        ".tgz",
        ".tar.xz",
        ".txz",
        ".tar.bz2",
        ".tbz2",
        ".tar",
        ".zip",
    ):
        if low.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    cleaned = _ARCH_OS_STRIP_RE.sub("", stem)
    match = _VERSION_RE.search(cleaned)
    return normalize_version(match.group(1)) if match else ""


class SourceProvider(ABC):
    """Abstract interface for resolving releases from an upstream source."""

    provider_id: str = ""

    @abstractmethod
    def matches_source(self, source: str) -> bool:
        """Return True if this provider handles the given source string."""

    @abstractmethod
    def resolve_latest(
        self,
        source: str,
        asset_pattern: str = "",
        current: AppRecord | None = None,
    ) -> ReleaseInfo:
        """Query the source and return metadata for the latest available release."""
