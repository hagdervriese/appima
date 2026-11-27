"""End-to-end integration tests for AppManager and CLI against an isolated temp env."""

from __future__ import annotations

import hashlib
import http.server
import tempfile
import threading
import unittest
from pathlib import Path

from appima.cli.main import main as cli_main
from appima.core.config import AppimaPaths
from appima.core.manager import AppManager
from appima.core.models import ChecksumMismatchError


def _fake_appimage_bytes(tag: bytes) -> bytes:
    """Create a minimal valid ELF header payload accepted by AppImageFormat."""
    return b"\x7fELF\x02\x01\x01" + b"\x00" * 57 + tag * 20


class _AppServerState:
    def __init__(self) -> None:
        self.payload = _fake_appimage_bytes(b"v1")
        self.etag = '"etag-v1"'
        self.filename = "SampleApp-1.0.0-x86_64.AppImage"


def _start_test_server(state: _AppServerState) -> tuple[http.server.ThreadingHTTPServer, str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def _send_headers(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(state.payload)))
            self.send_header("ETag", state.etag)
            self.send_header(
                "Content-Disposition", f'attachment; filename="{state.filename}"'
            )
            self.end_headers()

        def do_HEAD(self) -> None:  # noqa: N802
            self._send_headers()

        def do_GET(self) -> None:  # noqa: N802
            self._send_headers()
            self.wfile.write(state.payload)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}/latest.AppImage"


class ManagerEndToEndTests(unittest.TestCase):
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

    def test_local_install_checksum_and_adopt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mgr = self._make_manager(root)

            downloads = root / "Downloads"
            downloads.mkdir()
            src = downloads / "NotePad-2.1.0-x86_64.AppImage"
            data = _fake_appimage_bytes(b"notepad-2.1.0")
            src.write_bytes(data)
            md5 = hashlib.md5(data).hexdigest()

            # Wrong checksum must fail cleanly without creating DB entry or file
            with self.assertRaises(ChecksumMismatchError):
                mgr.install_app(str(src), expected_checksum="0" * 32)
            self.assertEqual(mgr.list_apps(), [])

            # Correct MD5 succeeds
            rec = mgr.install_app(str(src), expected_checksum=md5)
            self.assertEqual(rec.installed_version, "2.1.0")
            self.assertTrue(rec.checksum_verified)
            self.assertEqual(rec.checksum_algo, "md5")
            self.assertTrue(Path(rec.install_path).is_file())
            self.assertTrue(Path(rec.desktop_file).is_file())

            # Existing unmanaged file placed directly inside ~/Apps
            unmanaged_file = root / "Apps" / "ExistingTool-0.5.AppImage"
            unmanaged_file.write_bytes(_fake_appimage_bytes(b"existing"))
            found = mgr.scan_unmanaged()
            self.assertEqual(found, [unmanaged_file])

            # Adopting keeps it in-place without duplicating
            adopted = mgr.install_app(str(unmanaged_file))
            self.assertEqual(Path(adopted.install_path).resolve(), unmanaged_file.resolve())
            self.assertEqual(mgr.scan_unmanaged(), [])

    def test_remote_install_update_rollback_and_uninstall(self) -> None:
        state = _AppServerState()
        server, url = _start_test_server(state)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                mgr = self._make_manager(root)

                v1_sha256 = hashlib.sha256(state.payload).hexdigest()
                rec = mgr.install_app(url, expected_checksum=v1_sha256)
                self.assertEqual(rec.installed_version, "1.0.0")
                self.assertTrue(rec.checksum_verified)
                original_path = rec.install_path

                # No update yet
                res1 = mgr.check_update(rec.id)
                self.assertFalse(res1.update_available)

                # Publish v2.0.0 on the server
                state.payload = _fake_appimage_bytes(b"v2-new")
                state.etag = '"etag-v2"'
                state.filename = "SampleApp-2.0.0-x86_64.AppImage"

                res2 = mgr.check_update(rec.id)
                self.assertTrue(res2.update_available)
                self.assertEqual(res2.available_version, "2.0.0")

                # Update in place — path stays identical so pinned icons don't break
                updated = mgr.update_app(rec.id)
                self.assertEqual(updated.installed_version, "2.0.0")
                self.assertEqual(updated.install_path, original_path)
                self.assertTrue(updated.has_backup)
                self.assertEqual(updated.backup_version, "1.0.0")
                self.assertEqual(Path(original_path).read_bytes(), state.payload)

                # Rollback restores v1.0.0 bytes and version
                rolled = mgr.rollback_app(rec.id)
                self.assertEqual(rolled.installed_version, "1.0.0")
                self.assertFalse(rolled.has_backup)
                self.assertEqual(
                    Path(original_path).read_bytes(), _fake_appimage_bytes(b"v1")
                )

                # Launch settings update refreshes .desktop file
                edited = mgr.update_app_settings(
                    rec.id, launch_args="--enable-wayland", env_vars="DEBUG=1"
                )
                desktop_txt = Path(edited.desktop_file).read_text(encoding="utf-8")
                self.assertIn("env DEBUG=1", desktop_txt)
                self.assertIn("--enable-wayland", desktop_txt)

                # Uninstall cleans up binary, desktop file, and DB row
                desktop_path = Path(edited.desktop_file)
                mgr.uninstall_app(rec.id)
                self.assertFalse(Path(original_path).exists())
                self.assertFalse(desktop_path.exists())
                self.assertIsNone(mgr.get_app(rec.id))
        finally:
            server.shutdown()
            server.server_close()

    def test_cli_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mgr = self._make_manager(root)
            sample = root / "CliDemo-3.0.AppImage"
            sample.write_bytes(_fake_appimage_bytes(b"cli-demo"))

            self.assertEqual(cli_main(["install", str(sample)], manager=mgr), 0)
            self.assertEqual(cli_main(["list"], manager=mgr), 0)
            self.assertEqual(cli_main(["check"], manager=mgr), 0)
            self.assertEqual(cli_main(["uninstall", "clidemo"], manager=mgr), 0)
            self.assertEqual(mgr.list_apps(), [])


if __name__ == "__main__":
    unittest.main()
