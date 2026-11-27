"""Local filesystem provider for installing or adopting existing AppImages."""

from __future__ import annotations

from pathlib import Path

from appima.core.models import AppRecord, ProviderError, ReleaseInfo
from appima.providers.base import SourceProvider, extract_version_from_filename


class LocalFileProvider(SourceProvider):
    """Handles local .AppImage files, portable archives (.tar.*, .zip), and app directories."""

    provider_id = "local"

    def matches_source(self, source: str) -> bool:
        raw = source.strip()
        if not raw or raw.startswith(("http://", "https://")):
            return False
        p = Path(raw).expanduser()
        return p.is_file() or p.is_dir()

    def resolve_latest(
        self,
        source: str,
        asset_pattern: str = "",
        current: AppRecord | None = None,
    ) -> ReleaseInfo:
        path = Path(source.strip()).expanduser().resolve()
        if not (path.is_file() or path.is_dir()):
            raise ProviderError(f"Local path does not exist: {path}")
        version = extract_version_from_filename(path.name) or "local"
        size = path.stat().st_size if path.is_file() else None
        return ReleaseInfo(
            provider=self.provider_id,
            version=version,
            download_url=str(path),
            filename=path.name,
            size_bytes=size,
        )
