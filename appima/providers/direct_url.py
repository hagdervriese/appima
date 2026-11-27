"""Direct HTTP(S) download URL provider with ETag / Last-Modified tracking."""

from __future__ import annotations

import hashlib

from appima.core.downloader import head_request
from appima.core.models import AppRecord, ProviderError, ReleaseInfo
from appima.providers.base import SourceProvider, extract_version_from_filename


def _fingerprint(etag: str, last_modified: str) -> str:
    token = f"{etag}|{last_modified}".strip("|")
    if not token:
        return "unknown"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:10]


class DirectUrlProvider(SourceProvider):
    """Handles direct HTTP(S) links to AppImage binaries."""

    provider_id = "direct_url"

    def matches_source(self, source: str) -> bool:
        low = source.strip().lower()
        return low.startswith("http://") or low.startswith("https://")

    def resolve_latest(
        self,
        source: str,
        asset_pattern: str = "",
        current: AppRecord | None = None,
    ) -> ReleaseInfo:
        url = source.strip()
        if not self.matches_source(url):
            raise ProviderError(f"Not a valid HTTP(S) URL: {source!r}")

        head = head_request(url)
        filename = head.filename
        version = extract_version_from_filename(filename)

        if not version:
            # If the URL is a stable "latest" link, track ETag / Last-Modified changes
            if head.etag or head.last_modified:
                version = f"rev-{_fingerprint(head.etag, head.last_modified)}"
            else:
                version = "unknown"

        return ReleaseInfo(
            provider=self.provider_id,
            version=version,
            download_url=head.final_url,
            filename=filename,
            etag=head.etag,
            last_modified=head.last_modified,
            size_bytes=head.content_length,
            release_url=url,
        )
