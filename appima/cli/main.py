"""CLI entry point for appima (and default GUI launcher when invoked without args)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from appima import __version__
from appima.core.manager import AppManager
from appima.core.models import VALID_CHECK_INTERVALS, AppimaError
from appima.core.runtime import SELF_APP_ID
from appima.service.notify import notify_updates
from appima.service.systemd import get_timer_status, sync_update_timer


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="appima",
        description="Minimalist AppImage & portable app manager for Linux.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"appima {__version__}",
    )

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("gui", help="Launch the graphical Qt 6 manager (default)")
    sub.add_parser("list", help="List managed applications")

    p_install = sub.add_parser("install", help="Install an AppImage from URL, GitHub, or local file")
    p_install.add_argument("source", help="GitHub repo URL, direct download URL, or local file path")
    p_install.add_argument("--checksum", default="", help="Expected MD5/SHA-256/SHA-512 checksum")
    p_install.add_argument("--name", default="", help="Override application display name")
    p_install.add_argument("--pattern", default="", help="Glob pattern to select a specific GitHub asset")

    p_check = sub.add_parser("check", help="Check managed apps for upstream updates")
    p_check.add_argument("app_id", nargs="?", default="", help="Optional single app ID to check")
    p_check.add_argument(
        "--notify",
        action="store_true",
        help="Send a desktop notification if updates are available",
    )

    p_update = sub.add_parser("update", help="Update one app or all apps with available updates")
    p_update.add_argument("app_id", nargs="?", default="", help="App ID to update")
    p_update.add_argument("--all", action="store_true", help="Update every app with a newer release")
    p_update.add_argument("--checksum", default="", help="Optional expected checksum (single app)")

    p_rollback = sub.add_parser("rollback", help="Restore the backed-up previous version of an app")
    p_rollback.add_argument("app_id", help="App ID to roll back")

    p_uninstall = sub.add_parser("uninstall", help="Uninstall an app and remove its desktop entry")
    p_uninstall.add_argument("app_id", help="App ID to remove")

    p_reintegrate = sub.add_parser(
        "reintegrate", help="Refresh desktop entry and icon for an app (or --all)"
    )
    p_reintegrate.add_argument("app_id", nargs="?", default="", help="App ID to re-integrate")
    p_reintegrate.add_argument("--all", action="store_true", help="Refresh all managed apps")
    p_reintegrate.add_argument("--icon", default="", help="Optional custom icon file path")

    p_scan = sub.add_parser("scan", help="Find unmanaged AppImages inside your Apps folder")
    p_scan.add_argument("--adopt", action="store_true", help="Adopt all discovered AppImages")

    sub.add_parser(
        "integrate-self",
        help="Integrate the running appima AppImage into your Apps folder and desktop menu",
    )

    p_config = sub.add_parser("config", help="View or edit appima configuration")
    p_config.add_argument("--apps-dir", default=None, help="Directory where managed apps are stored")
    p_config.add_argument(
        "--interval",
        choices=VALID_CHECK_INTERVALS,
        default=None,
        help="Background update check interval",
    )

    return parser


def _progress_printer(downloaded: int, total: int | None) -> None:
    if total and total > 0:
        pct = min(100, int(downloaded * 100 / total))
        mb = downloaded / (1024 * 1024)
        total_mb = total / (1024 * 1024)
        sys.stderr.write(f"\rDownloading… {pct:3d}% ({mb:.1f} / {total_mb:.1f} MiB)")
    else:
        mb = downloaded / (1024 * 1024)
        sys.stderr.write(f"\rDownloading… {mb:.1f} MiB")
    sys.stderr.flush()


def main(argv: Sequence[str] | None = None, manager: AppManager | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command in (None, "gui"):
        from appima.gui.app import launch

        return launch()

    mgr = manager or AppManager()

    try:
        if args.command == "list":
            apps = mgr.list_apps()
            if not apps:
                print(f"No managed apps yet. Apps folder: {mgr.config.apps_dir}")
                return 0
            for app in apps:
                update_tag = (
                    f"  [update → {app.available_version}]" if app.has_update else ""
                )
                verified = "✓" if app.checksum_verified else "·"
                print(
                    f"{app.id:<22} {app.name:<24} v{app.installed_version:<14} "
                    f"[{app.provider}] {verified}{update_tag}"
                )
            return 0

        if args.command == "install":
            rec = mgr.install_app(
                args.source,
                expected_checksum=args.checksum,
                custom_name=args.name,
                asset_pattern=args.pattern,
                progress_cb=_progress_printer if sys.stderr.isatty() else None,
            )
            if sys.stderr.isatty():
                sys.stderr.write("\n")
            verified_str = (
                f" ({rec.checksum_algo.upper()} verified)"
                if rec.checksum_verified
                else ""
            )
            print(f"Installed {rec.name} ({rec.installed_version}) → {rec.install_path}{verified_str}")
            print(f"Desktop entry: {rec.desktop_file}")
            return 0

        if args.command == "check":
            results = (
                [mgr.check_update(args.app_id)]
                if args.app_id
                else mgr.check_all_updates()
            )
            updates = [r for r in results if r.update_available]
            for r in results:
                if r.error:
                    print(f"[error] {r.app_name}: {r.error}", file=sys.stderr)
                elif r.update_available:
                    print(
                        f"[update] {r.app_name}: {r.installed_version} → {r.available_version}"
                    )
                else:
                    print(f"[ok] {r.app_name}: {r.installed_version}")
            if args.notify and updates:
                notify_updates(updates)
            return 0

        if args.command == "update":
            if args.all:
                mgr.check_all_updates()
                targets = [a for a in mgr.list_apps() if a.has_update]
                if not targets:
                    print("All managed apps are up to date.")
                    return 0
                for app in targets:
                    rec = mgr.update_app(
                        app.id,
                        progress_cb=_progress_printer if sys.stderr.isatty() else None,
                    )
                    if sys.stderr.isatty():
                        sys.stderr.write("\n")
                    print(f"Updated {rec.name} → {rec.installed_version}")
                return 0

            if not args.app_id:
                print("Specify an app ID or pass --all.", file=sys.stderr)
                return 2
            rec = mgr.update_app(
                args.app_id,
                expected_checksum=args.checksum,
                progress_cb=_progress_printer if sys.stderr.isatty() else None,
            )
            if sys.stderr.isatty():
                sys.stderr.write("\n")
            print(f"Updated {rec.name} → {rec.installed_version}")
            return 0

        if args.command == "rollback":
            rec = mgr.rollback_app(args.app_id)
            print(f"Rolled back {rec.name} to {rec.installed_version}")
            return 0

        if args.command == "uninstall":
            mgr.uninstall_app(args.app_id)
            print(f"Uninstalled {args.app_id}")
            return 0

        if args.command == "reintegrate":
            if args.all:
                for app in mgr.list_apps():
                    mgr.reintegrate_app(app.id, reextract_icon=True)
                    print(f"Refreshed {app.name} ({app.id})")
                return 0
            if not args.app_id:
                print("Specify an app ID or pass --all.", file=sys.stderr)
                return 2
            rec = mgr.reintegrate_app(
                args.app_id,
                custom_icon_file=args.icon or None,
                reextract_icon=not bool(args.icon),
            )
            print(f"Refreshed {rec.name} → icon: {rec.icon_path or 'default'}")
            return 0

        if args.command == "scan":
            unmanaged = mgr.scan_unmanaged()
            if not unmanaged:
                print(f"No unmanaged AppImages found in {mgr.config.apps_dir}")
                return 0
            for path in unmanaged:
                if args.adopt:
                    rec = mgr.install_app(str(path))
                    print(f"Adopted {path.name} as {rec.name} ({rec.id})")
                else:
                    print(f"Unmanaged: {path}")
            return 0

        if args.command == "integrate-self":
            rec = mgr.integrate_self()
            if mgr.config.check_interval != "off":
                sync_update_timer(
                    mgr.config.check_interval,
                    mgr.paths,
                    appimage_path=Path(rec.install_path),
                )
            print(f"Integrated appima → {rec.install_path}")
            print(f"Desktop entry: {rec.desktop_file}")
            return 0

        if args.command == "config":
            changed = False
            if args.apps_dir is not None or args.interval is not None:
                mgr.update_config(
                    apps_dir=Path(args.apps_dir).expanduser() if args.apps_dir else None,
                    check_interval=args.interval,
                )
                changed = True
            if args.interval is not None:
                self_rec = mgr.get_app(SELF_APP_ID)
                self_bin = (
                    Path(self_rec.install_path)
                    if self_rec and self_rec.is_present
                    else None
                )
                ok, msg = sync_update_timer(
                    args.interval, mgr.paths, appimage_path=self_bin
                )
                print(msg)
                if not ok:
                    return 1
            if not changed or args.interval is None:
                print(f"Apps folder:    {mgr.config.apps_dir}")
                print(f"Update checks:  {mgr.config.check_interval}")
                print(f"Timer status:   {get_timer_status(mgr.paths)}")
            return 0

    except AppimaError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0
