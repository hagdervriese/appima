"""Comprehensive tests for portable archive bundles (.tar.gz, .zip, folders) and DB v1->v2 migration."""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import sqlite3
import struct
import tarfile
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from appima.core.config import AppimaPaths
from appima.core.database import Database
from appima.core.manager import AppManager
from appima.core.models import AppimaError, FormatError
from appima.providers.github import select_appimage_asset


def _make_elf(tag: bytes) -> bytes:
    return b"\x7fELF\x02\x01\x01" + b"\x00" * 57 + tag * 12


def _build_studio_tar_gz(dest: Path, version: str = "2026.1.0") -> None:
    """Create a synthetic JetBrains / Android Studio .tar.gz with nested root & product-info.json."""
    with tarfile.open(dest, "w:gz") as tf:
        def add_bytes(rel: str, data: bytes, mode: int = 0o644) -> None:
            info = tarfile.TarInfo(name=f"android-studio/{rel}")
            info.size = len(data)
            info.mode = mode
            tf.addfile(info, io.BytesIO(data))

        product = {
            "name": "Android Studio",
            "version": version,
            "svgIconPath": "bin/studio.svg",
            "launch": [{"launcherPath": "bin/studio"}],
        }
        add_bytes("product-info.json", json.dumps(product).encode("utf-8"))
        add_bytes("bin/studio", _make_elf(version.encode()), mode=0o755)
        add_bytes("bin/studio.sh", b"#!/bin/sh\nexec ./studio\n", mode=0o755)
        add_bytes("bin/fsnotifier", _make_elf(b"helper"), mode=0o755)
        add_bytes(
            "bin/studio.svg",
            b"<svg xmlns='http://www.w3.org/2000/svg'><circle r='10'/></svg>",
        )


def _build_electron_zip(dest: Path, version: str = "1.4.0") -> None:
    """Create a synthetic Antigravity-like Electron .zip with Unix executable bits."""
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        def add_file(rel: str, data: bytes, mode: int = 0o644) -> None:
            zi = zipfile.ZipInfo(f"Antigravity-x64/{rel}")
            zi.external_attr = (mode & 0xFFFF) << 16
            zf.writestr(zi, data)

        add_file("antigravity", _make_elf(b"antigravity-main"), mode=0o755)
        add_file("chrome-sandbox", _make_elf(b"sandbox"), mode=0o755)
        add_file("chrome_crashpad_handler", _make_elf(b"crashpad"), mode=0o755)
        add_file("libffmpeg.so", _make_elf(b"ffmpeg"), mode=0o755)
        add_file("version", (version + "\n").encode("utf-8"), mode=0o644)
        add_file(
            "resources/app/resources/linux/antigravity.png",
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
            mode=0o644,
        )


class _ArchiveServerState:
    def __init__(self, payload: bytes, filename: str, etag: str) -> None:
        self.payload = payload
        self.filename = filename
        self.etag = etag


def _start_archive_server(
    state: _ArchiveServerState,
) -> tuple[http.server.ThreadingHTTPServer, str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def _send_headers(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
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
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}/download"


class ArchiveFormatTests(unittest.TestCase):
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

    def test_install_tar_gz_jetbrains_and_zip_electron(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mgr = self._make_manager(root)

            # 1. Android Studio style .tar.gz
            studio_tar = root / "android-studio-2026.1.0-linux.tar.gz"
            _build_studio_tar_gz(studio_tar, version="2026.1.0")
            sha = hashlib.sha256(studio_tar.read_bytes()).hexdigest()

            rec = mgr.install_app(str(studio_tar), expected_checksum=sha)
            self.assertEqual(rec.format, "archive")
            self.assertEqual(rec.name, "Android Studio")
            self.assertEqual(rec.installed_version, "2026.1.0")
            self.assertTrue(rec.checksum_verified)
            self.assertTrue(Path(rec.install_path).is_dir())
            self.assertTrue(rec.exec_path.endswith("/bin/studio"))
            self.assertTrue(Path(rec.exec_path).is_file())
            self.assertTrue(rec.icon_path.endswith(".svg"))
            desktop_txt = Path(rec.desktop_file).read_text(encoding="utf-8")
            self.assertIn(f"Exec={rec.exec_path}", desktop_txt)

            # 2. Electron / Antigravity style .zip
            ag_zip = root / "Antigravity-x64.zip"
            _build_electron_zip(ag_zip, version="1.4.0")
            rec2 = mgr.install_app(str(ag_zip))
            self.assertEqual(rec2.format, "archive")
            self.assertEqual(rec2.name, "Antigravity")
            self.assertEqual(rec2.installed_version, "1.4.0")
            self.assertTrue(rec2.exec_path.endswith("/antigravity"))
            self.assertTrue(Path(rec2.exec_path).stat().st_mode & 0o111)
            self.assertTrue(rec2.icon_path.endswith(".png"))
            # Icons live in <apps_dir>/.icons/
            self.assertEqual(
                Path(rec2.icon_path).parent.resolve(), (root / "Apps" / ".icons").resolve()
            )

    def test_electron_app_asar_icon_extraction_and_dot_icons_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mgr = self._make_manager(root)

            # Build an Electron folder with icon ONLY inside resources/app.asar
            app_dir = root / "Apps" / "Antigravity-x64"
            res_dir = app_dir / "resources"
            res_dir.mkdir(parents=True)
            bin_path = app_dir / "antigravity"
            bin_path.write_bytes(_make_elf(b"ag"))
            bin_path.chmod(0o755)

            png_payload = b"\x89PNG\r\n\x1a\n" + b"real-icon-data" * 20
            decoy_payload = b"\x89PNG\r\n\x1a\n" + b"decoy" * 50
            header_obj = {
                "files": {
                    "icon.png": {"size": len(png_payload), "offset": "0"},
                    "node_modules": {
                        "files": {
                            "pkg": {
                                "files": {
                                    "icon.png": {
                                        "size": len(decoy_payload),
                                        "offset": str(len(png_payload)),
                                    }
                                }
                            }
                        }
                    },
                }
            }
            header_bytes = json.dumps(header_obj).encode("utf-8")
            str_len = len(header_bytes)
            padded_len = (str_len + 3) & ~3
            header_padded = header_bytes + b"\x00" * (padded_len - str_len)
            # Electron ASAR pickle header: [4, header_size, header_payload, str_len]
            header_size = 8 + padded_len
            pickle_hdr = struct.pack("<IIII", 4, header_size, 4 + padded_len, str_len)
            (res_dir / "app.asar").write_bytes(
                pickle_hdr + header_padded + png_payload + decoy_payload
            )

            rec = mgr.install_app(str(app_dir))
            icon_p = Path(rec.icon_path)
            self.assertTrue(icon_p.is_file())
            self.assertEqual(icon_p.parent.resolve(), (root / "Apps" / ".icons").resolve())
            self.assertEqual(icon_p.read_bytes(), png_payload)
            self.assertIn(
                f"Icon={icon_p}",
                Path(rec.desktop_file).read_text(encoding="utf-8"),
            )

            # Uninstall cleans up the icon in <apps_dir>/.icons too
            mgr.uninstall_app(rec.id)
            self.assertFalse(icon_p.exists())

    def test_zip_slip_traversal_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mgr = self._make_manager(root)

            bad_zip = root / "evil.zip"
            with zipfile.ZipFile(bad_zip, "w") as zf:
                zf.writestr("../escape.sh", "#!/bin/sh\necho pwned\n")
            with self.assertRaises(FormatError):
                mgr.install_app(str(bad_zip))

            bad_tar = root / "evil.tar.gz"
            with tarfile.open(bad_tar, "w:gz") as tf:
                info = tarfile.TarInfo(name="../../escape.sh")
                data = b"#!/bin/sh\n"
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            with self.assertRaises(FormatError):
                mgr.install_app(str(bad_tar))

    def test_remote_archive_update_rollback_exec_override_and_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mgr = self._make_manager(root)

            tar_v1 = root / "studio-1.0.0.tar.gz"
            tar_v2 = root / "studio-2.0.0.tar.gz"
            _build_studio_tar_gz(tar_v1, version="1.0.0")
            _build_studio_tar_gz(tar_v2, version="2.0.0")

            state = _ArchiveServerState(
                tar_v1.read_bytes(), "android-studio-1.0.0-linux.tar.gz", '"v1"'
            )
            server, url = _start_archive_server(state)
            try:
                rec = mgr.install_app(url)
                self.assertEqual(rec.installed_version, "1.0.0")
                self.assertTrue(Path(rec.exec_path).is_file())

                # Publish v2.0.0
                state.payload = tar_v2.read_bytes()
                state.filename = "android-studio-2.0.0-linux.tar.gz"
                state.etag = '"v2"'

                chk = mgr.check_update(rec.id)
                self.assertTrue(chk.update_available)
                self.assertEqual(chk.available_version, "2.0.0")

                updated = mgr.update_app(rec.id)
                self.assertEqual(updated.installed_version, "2.0.0")
                self.assertTrue(updated.has_backup)
                self.assertTrue(Path(updated.backup_path).is_dir())

                # Rollback restores v1.0.0 directory
                rolled = mgr.rollback_app(rec.id)
                self.assertEqual(rolled.installed_version, "1.0.0")
                self.assertFalse(rolled.has_backup)

                # Override executable to bin/studio.sh
                alt_exec = Path(rolled.install_path) / "bin" / "studio.sh"
                edited = mgr.update_app_settings(rec.id, exec_path=str(alt_exec))
                self.assertEqual(edited.exec_path, str(alt_exec))
                self.assertIn(
                    f"Exec={alt_exec}",
                    Path(edited.desktop_file).read_text(encoding="utf-8"),
                )

                # Clean uninstall removes directory + desktop entry
                install_dir = Path(edited.install_path)
                desktop_file = Path(edited.desktop_file)
                mgr.uninstall_app(rec.id)
                self.assertFalse(install_dir.exists())
                self.assertFalse(desktop_file.exists())
            finally:
                server.shutdown()
                server.server_close()

    def test_scan_and_adopt_extracted_folder_in_apps_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mgr = self._make_manager(root)

            app_folder = root / "Apps" / "GravityDesktop"
            app_folder.mkdir(parents=True)
            bin_path = app_folder / "gravity"
            bin_path.write_bytes(_make_elf(b"gravity"))
            bin_path.chmod(0o755)
            (app_folder / "version").write_text("0.9.5\n", encoding="utf-8")

            unmanaged = mgr.scan_unmanaged()
            self.assertEqual(unmanaged, [app_folder])

            preview = mgr.inspect_unmanaged(app_folder)
            self.assertEqual(preview.name, "GravityDesktop")
            self.assertEqual(preview.version, "0.9.5")
            self.assertEqual(Path(preview.exec_path).resolve(), bin_path.resolve())
            self.assertEqual(mgr.list_apps(), [])  # inspect must not modify DB

            adopted = mgr.install_app(str(app_folder))
            self.assertEqual(Path(adopted.install_path).resolve(), app_folder.resolve())
            self.assertEqual(Path(adopted.exec_path).resolve(), bin_path.resolve())
            self.assertEqual(adopted.installed_version, "0.9.5")
            self.assertEqual(mgr.scan_unmanaged(), [])

            # Test delete_unmanaged + safety guards
            extra = root / "Apps" / "Unused-1.0.AppImage"
            extra.write_bytes(_make_elf(b"unused"))
            mgr.delete_unmanaged(extra)
            self.assertFalse(extra.exists())

            # Refuses to delete outside Apps folder or hidden .icons folder
            outside = root / "outside.AppImage"
            outside.write_bytes(_make_elf(b"out"))
            with self.assertRaises(AppimaError):
                mgr.delete_unmanaged(outside)
            self.assertTrue(outside.exists())

            dot_icons = root / "Apps" / ".icons"
            dot_icons.mkdir(exist_ok=True)
            with self.assertRaises(AppimaError):
                mgr.delete_unmanaged(dot_icons)
            self.assertTrue(dot_icons.exists())

    def test_github_archive_fallback_selection(self) -> None:
        assets = [
            {"name": "Tool-1.0-windows-x64.zip", "browser_download_url": "https://x/win"},
            {"name": "Tool-1.0-macos-arm64.tar.gz", "browser_download_url": "https://x/mac"},
            {"name": "Tool-1.0-linux-x86_64.tar.gz", "browser_download_url": "https://x/lin64"},
            {"name": "Tool-1.0-linux-arm64.tar.gz", "browser_download_url": "https://x/linarm"},
        ]
        with patch("platform.machine", return_value="x86_64"):
            chosen = select_appimage_asset(assets)
            self.assertEqual(chosen["name"], "Tool-1.0-linux-x86_64.tar.gz")

    def test_database_v1_to_v2_migration_adds_exec_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "appima.db"
            # Create a legacy v1 database without exec_path
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                """
                CREATE TABLE apps (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    format TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    asset_pattern TEXT NOT NULL DEFAULT '',
                    installed_version TEXT NOT NULL,
                    available_version TEXT NOT NULL DEFAULT '',
                    available_download_url TEXT NOT NULL DEFAULT '',
                    available_checksum TEXT NOT NULL DEFAULT '',
                    available_checksum_algo TEXT NOT NULL DEFAULT '',
                    install_path TEXT NOT NULL,
                    backup_path TEXT NOT NULL DEFAULT '',
                    backup_version TEXT NOT NULL DEFAULT '',
                    desktop_file TEXT NOT NULL DEFAULT '',
                    icon_path TEXT NOT NULL DEFAULT '',
                    checksum_algo TEXT NOT NULL DEFAULT '',
                    checksum_value TEXT NOT NULL DEFAULT '',
                    checksum_verified INTEGER NOT NULL DEFAULT 0,
                    etag TEXT NOT NULL DEFAULT '',
                    last_modified TEXT NOT NULL DEFAULT '',
                    launch_args TEXT NOT NULL DEFAULT '',
                    env_vars TEXT NOT NULL DEFAULT '',
                    release_url TEXT NOT NULL DEFAULT '',
                    release_notes TEXT NOT NULL DEFAULT '',
                    installed_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    last_checked_at TEXT NOT NULL DEFAULT ''
                );
                """
            )
            conn.execute(
                "INSERT INTO apps (id, name, format, provider, source_url, installed_version, install_path) "
                "VALUES ('oldapp', 'Old App', 'appimage', 'local', '/tmp/a.AppImage', '1.0', '/tmp/a.AppImage')"
            )
            conn.execute("PRAGMA user_version = 1;")
            conn.commit()
            conn.close()

            db = Database(db_path)
            rec = db.get_app("oldapp")
            self.assertIsNotNone(rec)
            assert rec is not None
            self.assertEqual(rec.exec_path, "")
            self.assertEqual(rec.effective_exec_path, "/tmp/a.AppImage")


if __name__ == "__main__":
    unittest.main()
