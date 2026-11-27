"""Tests for XDG desktop entry integration and systemd user timer management."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from appima.core.config import AppimaPaths
from appima.core.desktop import (
    MANAGED_MARKER_KEY,
    build_exec_line,
    integrate_app,
    remove_integration,
)
from appima.core.models import AppRecord, ExtractedMetadata
from appima.service.systemd import SERVICE_NAME, TIMER_NAME, sync_update_timer


def _temp_paths(root: Path) -> AppimaPaths:
    paths = AppimaPaths(
        config_dir=root / "config" / "appima",
        data_dir=root / "data" / "appima",
        cache_dir=root / "cache" / "appima",
        applications_dir=root / "data" / "applications",
        systemd_user_dir=root / "config" / "systemd" / "user",
    )
    paths.ensure_dirs()
    return paths


class DesktopAndSystemdTests(unittest.TestCase):
    def test_build_exec_line_with_args_and_env(self) -> None:
        cmd = build_exec_line(
            "/home/user/My Apps/demo.AppImage",
            launch_args="--no-sandbox --ozone-platform=wayland",
            env_vars="FOO=bar",
        )
        self.assertEqual(
            cmd,
            'env FOO=bar "/home/user/My Apps/demo.AppImage" --no-sandbox --ozone-platform=wayland %U',
        )

    def test_integrate_and_safe_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _temp_paths(root)
            rec = AppRecord(
                id="demo-app",
                name="Demo App",
                format="appimage",
                provider="local",
                source_url="/tmp/demo.AppImage",
                installed_version="1.0.0",
                install_path=str(root / "Apps" / "demo-app.AppImage"),
                launch_args="--no-sandbox",
            )
            meta = ExtractedMetadata(
                name="Demo App",
                comment="A sample portable app",
                categories="Development;",
                icon_bytes=b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
                icon_ext="svg",
            )

            desktop_path, icon_path = integrate_app(
                rec, meta, paths, refresh_cache=False
            )
            self.assertTrue(desktop_path.is_file())
            self.assertIsNotNone(icon_path)
            assert icon_path is not None
            self.assertTrue(icon_path.is_file())

            text = desktop_path.read_text(encoding="utf-8")
            self.assertIn("Name=Demo App", text)
            self.assertIn(f"{MANAGED_MARKER_KEY}=true", text)
            self.assertIn("--no-sandbox", text)
            self.assertIn(f"Icon={icon_path}", text)

            rec.desktop_file = str(desktop_path)
            rec.icon_path = str(icon_path)
            remove_integration(rec, paths, refresh_cache=False)
            self.assertFalse(desktop_path.exists())
            self.assertFalse(icon_path.exists())

            # Safety guard: unmanaged .desktop file must never be deleted
            foreign = paths.applications_dir / "system-other.desktop"
            foreign.write_text("[Desktop Entry]\nName=Foreign\n", encoding="utf-8")
            rec.desktop_file = str(foreign)
            remove_integration(rec, paths, refresh_cache=False)
            self.assertTrue(foreign.exists())

    def test_systemd_timer_sync_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = _temp_paths(Path(tmp))

            ok, _ = sync_update_timer("daily", paths, run_systemctl=False)
            self.assertTrue(ok)
            timer_file = paths.systemd_user_dir / TIMER_NAME
            service_file = paths.systemd_user_dir / SERVICE_NAME
            self.assertTrue(timer_file.is_file())
            self.assertTrue(service_file.is_file())
            self.assertIn("OnCalendar=daily", timer_file.read_text(encoding="utf-8"))
            self.assertIn("check --notify", service_file.read_text(encoding="utf-8"))

            ok_off, _ = sync_update_timer("off", paths, run_systemctl=False)
            self.assertTrue(ok_off)
            self.assertFalse(timer_file.exists())
            self.assertFalse(service_file.exists())


if __name__ == "__main__":
    unittest.main()
