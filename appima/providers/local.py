"""Local filesystem provider for installing or adopting existing AppImages."""

from __future__ import annotations

from pathlib import Path

from appima.core.models import AppRecord, ProviderError, ReleaseInfo
from appima.providers.base import SourceProvider, extract_version_from_filename


class LocalFileProvider(SourceProvider):
    """Handles local .AppImage paths on disk."""

    provider_id = "local"

    def matches_source(self, source: str) -> bool:
        raw = source.strip()
        if not raw or raw.startswith(("http://", "https://")):
            return False
        return Path(raw).expanduser().is_file()

    def resolve_latest(
        self,
        source: str,
        asset_pattern: str = "",
        current: AppRecord | None = None,
    ) -> ReleaseInfo:
        path = Path(source.strip()).expanduser().resolve()
        if not path.is_file():
            raise ProviderError(f"Local file does not exist: {path}")
        version = extract_version_from_filename(path.name) or "local"
        return ReleaseInfo(
            provider=self.provider_id,
            version=version,
            download_url=str(path),
            filename=path.name,
            size_bytes=path.stat().st_size,
        )
