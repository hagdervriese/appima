"""Tests for provider URL parsing, asset selection, and version comparison."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from appima.core.models import ProviderError
from appima.providers.base import (
    extract_version_from_filename,
    is_newer_version,
    normalize_version,
)
from appima.providers.github import (
    find_checksum_asset_url,
    parse_github_repo,
    select_appimage_asset,
)


class ProviderTests(unittest.TestCase):
    def test_parse_github_repo(self) -> None:
        self.assertEqual(
            parse_github_repo("https://github.com/obsidianmd/obsidian-releases"),
            ("obsidianmd", "obsidian-releases"),
        )
        self.assertEqual(
            parse_github_repo("https://github.com/owner/repo/releases/latest"),
            ("owner", "repo"),
        )
        self.assertEqual(parse_github_repo("owner/repo"), ("owner", "repo"))
        # Direct binary download URL should not be hijacked as a repo URL
        self.assertIsNone(
            parse_github_repo(
                "https://github.com/owner/repo/releases/download/v1.0/App.AppImage"
            )
        )

    def test_version_comparison(self) -> None:
        self.assertEqual(normalize_version("v1.2.3"), "1.2.3")
        self.assertEqual(normalize_version("release-2.0"), "2.0")
        self.assertTrue(is_newer_version("1.9.0", "1.10.0"))
        self.assertTrue(is_newer_version("v1.2.3", "v1.2.4"))
        self.assertFalse(is_newer_version("1.10.0", "1.9.0"))
        self.assertFalse(is_newer_version("v1.2.3", "1.2.3"))
        self.assertTrue(is_newer_version("rev-aaaa1111", "rev-bbbb2222"))
        self.assertEqual(
            extract_version_from_filename("MyTool-2.4.1-x86_64.AppImage"), "2.4.1"
        )

    def test_select_appimage_asset_prefers_host_arch(self) -> None:
        assets = [
            {"name": "Tool-1.0-arm64.AppImage", "browser_download_url": "https://x/arm64"},
            {"name": "Tool-1.0-x86_64.AppImage.zsync", "browser_download_url": "https://x/zsync"},
            {"name": "Tool-1.0-x86_64.AppImage", "browser_download_url": "https://x/x86_64"},
            {"name": "SHA256SUMS", "browser_download_url": "https://x/sums"},
        ]
        with patch("platform.machine", return_value="x86_64"):
            chosen = select_appimage_asset(assets)
            self.assertEqual(chosen["name"], "Tool-1.0-x86_64.AppImage")

        with patch("platform.machine", return_value="aarch64"):
            chosen_arm = select_appimage_asset(assets)
            self.assertEqual(chosen_arm["name"], "Tool-1.0-arm64.AppImage")

        chosen_pat = select_appimage_asset(assets, asset_pattern="*arm64*")
        self.assertEqual(chosen_pat["name"], "Tool-1.0-arm64.AppImage")

        with self.assertRaises(ProviderError):
            select_appimage_asset([{"name": "notes.txt", "browser_download_url": "x"}])

    def test_find_checksum_asset_url(self) -> None:
        assets = [
            {"name": "Tool-1.0.AppImage", "browser_download_url": "https://x/app"},
            {"name": "Tool-1.0.AppImage.sha256", "browser_download_url": "https://x/app.sha256"},
            {"name": "SHA256SUMS", "browser_download_url": "https://x/SHA256SUMS"},
        ]
        self.assertEqual(
            find_checksum_asset_url(assets, "Tool-1.0.AppImage"),
            "https://x/app.sha256",
        )
        self.assertEqual(
            find_checksum_asset_url(assets[:1] + assets[2:], "Tool-1.0.AppImage"),
            "https://x/SHA256SUMS",
        )


if __name__ == "__main__":
    unittest.main()
