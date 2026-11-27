"""GitHub Releases provider with automatic architecture & checksum discovery."""

from __future__ import annotations

import fnmatch
import platform
import re
from typing import Any

from appima.core.checksum import parse_checksum_file
from appima.core.downloader import fetch_json, fetch_text
from appima.core.models import AppRecord, ProviderError, ReleaseInfo
from appima.providers.base import SourceProvider, normalize_version

_GITHUB_REPO_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/.*)?$"
)
_SHORTHAND_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")
_IGNORED_EXTENSIONS = (
    ".zsync",
    ".sha256",
    ".sha256sum",
    ".sha512",
    ".md5",
    ".asc",
    ".sig",
    ".blockmap",
    ".txt",
)
_ARCHIVE_EXTENSIONS = (
    ".tar.gz",
    ".tgz",
    ".tar.xz",
    ".txz",
    ".tar.bz2",
    ".tbz2",
    ".zip",
)
_NON_LINUX_HINTS = (
    "win32",
    "win64",
    "windows",
    "macos",
    "darwin",
    "osx",
    "-mac-",
    "_mac_",
    ".dmg",
    ".exe",
    ".msi",
    ".pkg",
    ".deb",
    ".rpm",
    "source",
)
_CHECKSUM_FILE_HINTS = (
    "sha256",
    "sha512",
    "md5",
    "checksum",
    "shasums",
)


def parse_github_repo(source: str) -> tuple[str, str] | None:
    """Parse owner/repo from a GitHub URL or shorthand (excludes direct file downloads)."""
    raw = source.strip()
    low = raw.lower()
    if "/releases/download/" in low and (
        low.endswith(".appimage")
        or any(low.endswith(ext) for ext in _ARCHIVE_EXTENSIONS)
    ):
        # Direct binary/archive URL: let DirectUrlProvider handle exact pinned files
        return None
    m = _GITHUB_REPO_RE.match(raw)
    if m:
        return m.group(1), m.group(2)
    m2 = _SHORTHAND_RE.match(raw)
    if m2 and not raw.startswith(("/", ".", "~")):
        return m2.group(1), m2.group(2)
    return None


def _host_arch_aliases() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (preferred_tokens, excluded_tokens) for the current CPU architecture."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return (
            ("x86_64", "amd64", "x64", "x86-64"),
            ("aarch64", "arm64", "armv7", "armhf", "i386", "i686", "riscv64"),
        )
    if machine in ("aarch64", "arm64"):
        return (
            ("aarch64", "arm64"),
            ("x86_64", "amd64", "x64", "x86-64", "i386", "i686", "riscv64"),
        )
    return ((), ())


def _pick_by_arch(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    preferred, excluded = _host_arch_aliases()
    for asset in candidates:
        low = str(asset["name"]).lower()
        if any(tok in low for tok in preferred):
            return asset
    neutral = [
        a
        for a in candidates
        if not any(tok in str(a["name"]).lower() for tok in excluded)
    ]
    return neutral[0] if neutral else candidates[0]


def select_appimage_asset(
    assets: list[dict[str, Any]],
    asset_pattern: str = "",
) -> dict[str, Any]:
    """Pick the best matching .AppImage or Linux portable archive from a GitHub release."""
    appimages: list[dict[str, Any]] = []
    archives: list[dict[str, Any]] = []
    for asset in assets:
        name = str(asset.get("name") or "")
        low = name.lower()
        if any(low.endswith(ext) for ext in _IGNORED_EXTENSIONS):
            continue
        if low.endswith(".appimage"):
            appimages.append(asset)
        elif any(low.endswith(ext) for ext in _ARCHIVE_EXTENSIONS):
            if not any(bad in low for bad in _NON_LINUX_HINTS):
                archives.append(asset)

    all_supported = appimages + archives
    if not all_supported:
        raise ProviderError(
            "No .AppImage or Linux portable archive asset found in the latest GitHub release."
        )

    if asset_pattern.strip():
        pat = asset_pattern.strip().lower()
        matched = [
            a for a in all_supported if fnmatch.fnmatch(str(a["name"]).lower(), pat)
        ]
        if not matched:
            names = ", ".join(str(a["name"]) for a in all_supported)
            raise ProviderError(
                f"No release asset matched pattern {asset_pattern!r}. Available: {names}"
            )
        return matched[0]

    if appimages:
        return _pick_by_arch(appimages)

    # Prefer archives explicitly tagged for Linux when falling back to tar/zip
    linux_tagged = [a for a in archives if "linux" in str(a["name"]).lower()]
    return _pick_by_arch(linux_tagged or archives)


def find_checksum_asset_url(
    assets: list[dict[str, Any]], target_filename: str
) -> str:
    """Find a likely companion checksum asset URL for target_filename, if published."""
    target_low = target_filename.lower()
    # Exact companion like MyApp.AppImage.sha256 or MyApp.AppImage.md5
    for asset in assets:
        name = str(asset.get("name") or "")
        low = name.lower()
        if low.startswith(target_low) and any(
            low.endswith(ext) for ext in (".sha256", ".sha256sum", ".sha512", ".md5")
        ):
            return str(asset.get("browser_download_url") or "")

    # Shared checksum manifest (SHA256SUMS, checksums.txt, MD5SUMS, ...)
    for asset in assets:
        name = str(asset.get("name") or "")
        low = name.lower()
        if low.endswith(".appimage") or low.endswith((".asc", ".sig", ".zsync")):
            continue
        if any(hint in low for hint in _CHECKSUM_FILE_HINTS):
            return str(asset.get("browser_download_url") or "")

    return ""


class GitHubReleasesProvider(SourceProvider):
    """Resolves AppImage releases from public (or token-authenticated) GitHub repos."""

    provider_id = "github"

    def __init__(self, token: str = "") -> None:
        self.token = token

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        if self.token.strip():
            headers["Authorization"] = f"Bearer {self.token.strip()}"
        return headers

    def matches_source(self, source: str) -> bool:
        return parse_github_repo(source) is not None

    def resolve_latest(
        self,
        source: str,
        asset_pattern: str = "",
        current: AppRecord | None = None,
    ) -> ReleaseInfo:
        parsed = parse_github_repo(source)
        if not parsed:
            raise ProviderError(f"Not a valid GitHub repository reference: {source!r}")
        owner, repo = parsed

        release = self._fetch_latest_release(owner, repo)
        assets = release.get("assets") or []
        chosen = select_appimage_asset(assets, asset_pattern=asset_pattern)

        filename = str(chosen["name"])
        download_url = str(chosen["browser_download_url"])
        tag = str(release.get("tag_name") or release.get("name") or "unknown")
        version = normalize_version(tag)
        size_bytes = chosen.get("size")

        # Try automatic checksum discovery (non-fatal if absent or unreadable)
        checksum = ""
        checksum_algo = ""
        checksum_url = find_checksum_asset_url(assets, filename)
        if checksum_url:
            try:
                text = fetch_text(checksum_url, headers=self._headers(), timeout=15)
                found = parse_checksum_file(text, filename)
                if found:
                    checksum_algo, checksum = found
            except ProviderError:
                pass

        return ReleaseInfo(
            provider=self.provider_id,
            version=version,
            download_url=download_url,
            filename=filename,
            name_hint=repo,
            checksum=checksum,
            checksum_algo=checksum_algo,
            checksum_url=checksum_url,
            release_url=str(release.get("html_url") or f"https://github.com/{owner}/{repo}"),
            release_notes=str(release.get("body") or ""),
            size_bytes=int(size_bytes) if isinstance(size_bytes, int) else None,
            asset_pattern=asset_pattern,
        )

    def _fetch_latest_release(self, owner: str, repo: str) -> dict[str, Any]:
        latest_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
        try:
            data = fetch_json(latest_url, headers=self._headers())
            if isinstance(data, dict) and data.get("assets") is not None:
                return data
        except ProviderError as exc:
            # Fallback if repo only has pre-releases or no 'latest' release
            if "HTTP 404" not in str(exc):
                raise

        list_url = f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=10"
        releases = fetch_json(list_url, headers=self._headers())
        if not isinstance(releases, list) or not releases:
            raise ProviderError(f"No releases found for GitHub repo {owner}/{repo}.")

        for rel in releases:
            if not rel.get("draft") and not rel.get("prerelease"):
                return rel
        for rel in releases:
            if not rel.get("draft"):
                return rel
        raise ProviderError(f"No published releases found for {owner}/{repo}.")
