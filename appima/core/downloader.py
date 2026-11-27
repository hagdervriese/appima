"""Pure-stdlib HTTP client and streaming file downloader."""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from appima import __version__
from appima.core.models import ProviderError

USER_AGENT = f"appima/{__version__} (+https://github.com/appima/appima)"
ProgressCallback = Callable[[int, int | None], None]

_CONTENT_DISP_RE = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";\r\n]+)"?', re.I)


@dataclass
class DownloadResult:
    """Metadata returned from a completed HTTP file download."""

    path: Path
    filename: str
    final_url: str
    etag: str = ""
    last_modified: str = ""
    content_length: int | None = None


@dataclass
class HeadResult:
    """Metadata returned from an HTTP HEAD probe."""

    final_url: str
    etag: str = ""
    last_modified: str = ""
    content_length: int | None = None
    filename: str = ""


def _build_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    if extra:
        headers.update(extra)
    return headers


def _extract_filename(url: str, content_disposition: str | None) -> str:
    if content_disposition:
        match = _CONTENT_DISP_RE.search(content_disposition)
        if match:
            name = urllib.parse.unquote(match.group(1)).strip()
            if name:
                return Path(name).name
    parsed = urllib.parse.urlparse(url)
    candidate = urllib.parse.unquote(Path(parsed.path).name)
    return candidate or "download.AppImage"


def fetch_text(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
) -> str:
    """Fetch UTF-8 text from an HTTP(S) URL."""
    req = urllib.request.Request(url, headers=_build_headers(headers))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"HTTP {exc.code} while requesting {url}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"Network error while requesting {url}: {exc.reason}") from exc


def fetch_json(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
) -> Any:
    """Fetch and decode JSON from an HTTP(S) URL."""
    merged = {"Accept": "application/json"}
    if headers:
        merged.update(headers)
    text = fetch_text(url, headers=merged, timeout=timeout)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"Invalid JSON response from {url}") from exc


def head_request(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
) -> HeadResult:
    """Probe URL headers via HEAD (falling back to minimal GET if HEAD is rejected)."""
    req = urllib.request.Request(url, headers=_build_headers(headers), method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _head_from_response(resp)
    except urllib.error.HTTPError as exc:
        if exc.code in (405, 501):
            get_req = urllib.request.Request(
                url, headers=_build_headers({**(headers or {}), "Range": "bytes=0-0"})
            )
            try:
                with urllib.request.urlopen(get_req, timeout=timeout) as resp:
                    return _head_from_response(resp)
            except (urllib.error.HTTPError, urllib.error.URLError) as inner:
                raise ProviderError(f"Failed to probe {url}: {inner}") from inner
        raise ProviderError(f"HTTP {exc.code} while probing {url}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"Network error while probing {url}: {exc.reason}") from exc


def _head_from_response(resp: Any) -> HeadResult:
    final_url = resp.geturl()
    cl_raw = resp.headers.get("Content-Length")
    content_length = int(cl_raw) if cl_raw and cl_raw.isdigit() else None
    return HeadResult(
        final_url=final_url,
        etag=(resp.headers.get("ETag") or "").strip(),
        last_modified=(resp.headers.get("Last-Modified") or "").strip(),
        content_length=content_length,
        filename=_extract_filename(final_url, resp.headers.get("Content-Disposition")),
    )


def download_file(
    url: str,
    dest_dir: Path,
    filename: str | None = None,
    progress_cb: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> DownloadResult:
    """Stream a file from URL into dest_dir atomically."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers=_build_headers(headers))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final_url = resp.geturl()
            resolved_name = filename or _extract_filename(
                final_url, resp.headers.get("Content-Disposition")
            )
            cl_raw = resp.headers.get("Content-Length")
            total_bytes = int(cl_raw) if cl_raw and cl_raw.isdigit() else None
            etag = (resp.headers.get("ETag") or "").strip()
            last_modified = (resp.headers.get("Last-Modified") or "").strip()

            part_path = dest_dir / f".{resolved_name}.part"
            final_path = dest_dir / resolved_name
            downloaded = 0

            try:
                with part_path.open("wb") as out:
                    while True:
                        if cancel_event and cancel_event.is_set():
                            raise ProviderError("Download cancelled by user.")
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb:
                            progress_cb(downloaded, total_bytes)
                part_path.replace(final_path)
            except Exception:
                part_path.unlink(missing_ok=True)
                raise

            return DownloadResult(
                path=final_path,
                filename=resolved_name,
                final_url=final_url,
                etag=etag,
                last_modified=last_modified,
                content_length=total_bytes or downloaded,
            )
    except ProviderError:
        raise
    except urllib.error.HTTPError as exc:
        raise ProviderError(
            f"HTTP {exc.code} while downloading {url}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ProviderError(
            f"Network error while downloading {url}: {exc.reason}"
        ) from exc
