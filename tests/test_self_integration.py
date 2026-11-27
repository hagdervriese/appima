"""Tests for AppImage self-integration and AppImage-mode systemd unit rendering."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from appima import __version__
from appima.core.config import AppimaPaths
from appima.core.manager import AppManager
from appima.core.runtime import SELF_APP_ID, SELF_GITHUB_REPO
from appima.service.systemd import render_service_unit, sync_update_timer


def _fake_appimage_bytes() -> bytes:
    return b"\x7fELF\x02\x01\x01" + b"\x00" * 57 + b"appima-binary-payload" * 16


class SelfIntegrationTests(unittest.TestCase):
    def _make_manager(self, root: Path) -> AppManager:
        paths = AppimaPaths(
            config_dir=root / "config" / "appima",
            data_dir=root / "data" / "appima",
            cache_dir=root / "cache" / "appima",
            applications_dir=root / "data" / "applications",
            systemd_user_dir=root / "config" / "systemd" / "user",
        )
        mgr = AppManager(paths=paths, refresh_desktop=False)
        mgr.update_config(apps_dir=root / "Apps")
        return mgr

    def test_integrate_self_from_downloads_and_systemd_exec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mgr = self._make_manager(root)

            downloads = root / "Downloads"
            downloads.mkdir()
            running = downloads / f"appima-{__version__}-x86_64.AppImage"
            running.write_bytes(_fake_appimage_bytes())

            self.assertTrue(mgr.needs_self_integration(appimage_path=running))

            rec = mgr.integrate_self(appimage_path=running)
            self.assertEqual(rec.id, SELF_APP_ID)
            self.assertEqual(rec.name, "appima")
            self.assertEqual(rec.provider, "github")
            self.assertEqual(rec.source_url, SELF_GITHUB_REPO)
            self.assertEqual(rec.installed_version, __version__)

            expected_bin = root / "Apps" / "appima.AppImage"
            self.assertEqual(Path(rec.install_path), expected_bin)
            self.assertTrue(expected_bin.is_file())
            self.assertTrue(Path(rec.desktop_file).is_file())
            self.assertTrue(Path(rec.icon_path).is_file())

            # Once integrated, prompt should no longer appear
            self.assertFalse(mgr.needs_self_integration(appimage_path=running))

            # Systemd service unit uses the AppImage directly (no python3/PYTHONPATH)
            unit_text = render_service_unit(appimage_path=expected_bin)
            self.assertIn(f'ExecStart="{expected_bin}" check --notify', unit_text)
            self.assertNotIn("PYTHONPATH", unit_text)

            ok, _ = sync_update_timer(
                "daily", mgr.paths, run_systemctl=False, appimage_path=expected_bin
            )
            self.assertTrue(ok)
            written = (mgr.paths.systemd_user_dir / "appima-check.service").read_text(
                encoding="utf-8"
            )
            self.assertIn(f'ExecStart="{expected_bin}" check --notify', written)

    def test_integrate_self_in_place_when_already_in_apps_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mgr = self._make_manager(root)

            apps_dir = root / "Apps"
            apps_dir.mkdir(parents=True)
            already_there = apps_dir / "appima-0.1.0-x86_64.AppImage"
            already_there.write_bytes(_fake_appimage_bytes())

            rec = mgr.integrate_self(appimage_path=already_there)
            self.assertEqual(Path(rec.install_path).resolve(), already_there.resolve())
            self.assertFalse(mgr.needs_self_integration(appimage_path=already_there))


if __name__ == "__main__":
    unittest.main()
