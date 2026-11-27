"""Pluggable update and installation source providers."""

from appima.providers.base import (
    SourceProvider,
    extract_version_from_filename,
    is_newer_version,
    normalize_version,
    slugify_app_id,
)
from appima.providers.direct_url import DirectUrlProvider
from appima.providers.github import GitHubReleasesProvider
from appima.providers.local import LocalFileProvider

__all__ = [
    "SourceProvider",
    "GitHubReleasesProvider",
    "DirectUrlProvider",
    "LocalFileProvider",
    "extract_version_from_filename",
    "is_newer_version",
    "normalize_version",
    "slugify_app_id",
]
