"""High-level AppManager orchestrating install, integration, updates, and rollback."""

from __future__ import annotations

import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from appima import __version__
from appima.core.checksum import compute_file_hash, verify_file_hash
from appima.core.config import AppimaPaths, load_config, save_config
from appima.core.database import Database
from appima.core.desktop import (
    execute_local_removal_plan,
    find_existing_desktop_entries,
    has_exact_desktop_name_conflict,
    integrate_app,
    plan_conflict_removal,
    remove_integration,
    suggest_unique_app_name,
)
from appima.core.downloader import ProgressCallback, download_file
from appima.core.models import (
    AppimaError,
    AppRecord,
    ChecksumMismatchError,
    Config,
    DesktopConflict,
    ExtractedMetadata,
    FormatError,
    ProviderError,
    ReleaseInfo,
    RemovalPlan,
    UnmanagedPreview,
    UpdateCheckResult,
)
from appima.core.runtime import (
    SELF_APP_ID,
    SELF_GITHUB_REPO,
    current_appimage_path,
    gui_asset_path,
)
from appima.formats import AppImageFormat, ArchiveFormat, PackageFormat
from appima.providers import (
    DirectUrlProvider,
    GitHubReleasesProvider,
    LocalFileProvider,
    SourceProvider,
    is_newer_version,
    normalize_version,
    slugify_app_id,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AppManager:
    """Central manager coordinating storage, formats, providers, and desktop integration."""

    def __init__(
        self,
        paths: AppimaPaths | None = None,
        providers: list[SourceProvider] | None = None,
        formats: list[PackageFormat] | None = None,
        refresh_desktop: bool = True,
    ) -> None:
        self.paths = paths or AppimaPaths.default()
        self.paths.ensure_dirs()
        self.config = load_config(self.paths)
        self.db = Database(self.paths.db_file)
        self.refresh_desktop = refresh_desktop
        self._formats: list[PackageFormat] = formats or [
            AppImageFormat(),
            ArchiveFormat(),
        ]
        self._custom_providers = providers

    @property
    def icons_dir(self) -> Path:
        """Directory storing extracted app icons alongside managed apps (<apps_dir>/.icons)."""
        return self.config.apps_dir.expanduser() / ".icons"

    @property
    def providers(self) -> list[SourceProvider]:
        if self._custom_providers is not None:
            return self._custom_providers
        return [
            LocalFileProvider(),
            GitHubReleasesProvider(token=self.config.github_token),
            DirectUrlProvider(),
        ]

    def reload_config(self) -> Config:
        self.config = load_config(self.paths)
        return self.config

    def update_config(
        self,
        apps_dir: Path | str | None = None,
        check_interval: str | None = None,
        keep_backup: bool | None = None,
        github_token: str | None = None,
    ) -> Config:
        if apps_dir is not None:
            self.config.apps_dir = Path(apps_dir).expanduser()
        if check_interval is not None:
            self.config.check_interval = check_interval
        if keep_backup is not None:
            self.config.keep_backup = keep_backup
        if github_token is not None:
            self.config.github_token = github_token
        save_config(self.config, self.paths)
        return self.config

    def _resolve_provider(self, source: str) -> SourceProvider:
        for provider in self.providers:
            if provider.matches_source(source):
                return provider
        raise ProviderError(
            f"Could not determine how to fetch {source!r}. "
            "Provide a GitHub repo URL, a direct http(s) download URL, or a local file path."
        )

    def _provider_by_id(self, provider_id: str) -> SourceProvider | None:
        for provider in self.providers:
            if provider.provider_id == provider_id:
                return provider
        return None

    def _resolve_format(self, file_path: Path) -> PackageFormat:
        for fmt in self._formats:
            if fmt.can_handle(file_path):
                return fmt
        raise FormatError(f"Unsupported package format for file: {file_path.name}")

    def _format_by_id(self, format_id: str) -> PackageFormat:
        for fmt in self._formats:
            if fmt.format_id == format_id:
                return fmt
        raise FormatError(f"Unknown package format: {format_id}")

    def list_apps(self) -> list[AppRecord]:
        return self.db.list_apps()

    def get_app(self, app_id: str) -> AppRecord | None:
        return self.db.get_app(app_id)

    def preview_source(self, source: str, asset_pattern: str = "") -> ReleaseInfo:
        """Resolve source metadata without downloading or installing anything."""
        provider = self._resolve_provider(source)
        release = provider.resolve_latest(source, asset_pattern=asset_pattern)
        # For local AppImages and already-extracted folders, inspect real display name safely
        if provider.provider_id == "local":
            local_path = Path(release.download_url)
            try:
                fmt = self._resolve_format(local_path)
                # Avoid extracting huge compressed archives twice during preview;
                # inspect directories and AppImages immediately.
                if fmt.format_id == "appimage" or local_path.is_dir():
                    meta = fmt.inspect(local_path)
                    if meta.name:
                        release.name_hint = meta.name
                    if release.version in ("", "local", "unknown") and meta.version_hint:
                        release.version = normalize_version(meta.version_hint)
            except AppimaError:
                pass
        return release

    def find_conflicting_apps(
        self,
        app_name: str,
        exclude_app_id: str = "",
        search_dirs: list[Path] | None = None,
    ) -> list[DesktopConflict]:
        """Find existing desktop entries with the same or a very similar name."""
        return find_existing_desktop_entries(
            app_name,
            self.paths,
            exclude_app_id=exclude_app_id,
            search_dirs=search_dirs,
        )

    def has_exact_name_conflict(
        self,
        app_name: str,
        exclude_app_id: str = "",
        search_dirs: list[Path] | None = None,
    ) -> DesktopConflict | None:
        """Return an existing desktop entry whose visible Name matches app_name exactly."""
        return has_exact_desktop_name_conflict(
            app_name,
            self.paths,
            exclude_app_id=exclude_app_id,
            search_dirs=search_dirs,
        )

    def suggest_unique_name(
        self,
        base_name: str,
        version: str = "",
        search_dirs: list[Path] | None = None,
    ) -> str:
        """Return a unique display name suggestion for a conflicting app."""
        return suggest_unique_app_name(
            base_name, version, self.paths, search_dirs=search_dirs
        )

    def plan_conflict_removal(
        self,
        conflict: DesktopConflict,
        home_dir: Path | None = None,
    ) -> RemovalPlan:
        """Return a transparent removal plan for an existing conflicting app."""
        return plan_conflict_removal(
            conflict,
            self.paths,
            self.config.apps_dir,
            home_dir=home_dir,
        )

    def remove_conflict(
        self,
        conflict: DesktopConflict,
        home_dir: Path | None = None,
    ) -> RemovalPlan:
        """Remove an existing conflicting app (appima-managed, Flatpak, or local entry)."""
        plan = self.plan_conflict_removal(conflict, home_dir=home_dir)
        if not plan.can_remove:
            raise AppimaError(
                plan.reason or "This entry cannot be removed automatically by appima."
            )
        if plan.method == "appima" and plan.managed_app_id:
            self.uninstall_app(plan.managed_app_id)
            return plan
        execute_local_removal_plan(
            plan, self.paths, refresh_cache=self.refresh_desktop
        )
        return plan

    def install_app(
        self,
        source: str,
        expected_checksum: str = "",
        custom_name: str = "",
        asset_pattern: str = "",
        progress_cb: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> AppRecord:
        """Download (if remote), verify checksum, install into apps_dir, and integrate."""
        provider = self._resolve_provider(source)
        release = provider.resolve_latest(source, asset_pattern=asset_pattern)

        staged_path: Path
        cleanup_staged = False
        etag = release.etag
        last_modified = release.last_modified
        in_place = False

        if provider.provider_id == "local":
            staged_path = Path(release.download_url).resolve()
            # If the file already sits in apps_dir, adopt it in place without duplicating
            try:
                if staged_path.parent == self.config.apps_dir.expanduser().resolve():
                    in_place = True
            except OSError:
                pass
        else:
            dl = download_file(
                release.download_url,
                self.paths.staging_dir,
                filename=release.filename,
                progress_cb=progress_cb,
                cancel_event=cancel_event,
            )
            staged_path = dl.path
            cleanup_staged = True
            etag = dl.etag or etag
            last_modified = dl.last_modified or last_modified

        try:
            # Determine checksum to verify: user-provided takes priority over auto-discovered
            checksum_input = expected_checksum.strip()
            if not checksum_input and release.checksum:
                checksum_input = (
                    f"{release.checksum_algo}:{release.checksum}"
                    if release.checksum_algo
                    else release.checksum
                )

            checksum_algo = "sha256"
            checksum_value = ""
            checksum_verified = False
            if staged_path.is_file():
                if checksum_input:
                    ok, actual, expected, algo = verify_file_hash(staged_path, checksum_input)
                    if not ok:
                        raise ChecksumMismatchError(expected, actual, algo)
                    checksum_algo = algo
                    checksum_value = actual
                    checksum_verified = True
                else:
                    checksum_value = compute_file_hash(staged_path, "sha256")
            elif checksum_input:
                raise AppimaError("Cannot verify a file checksum against a directory.")

            fmt = self._resolve_format(staged_path)

            if fmt.format_id == "archive" and staged_path.is_file():
                # Extract archive once into apps_dir, then inspect the extracted directory
                preliminary_name = (
                    custom_name.strip()
                    or release.name_hint
                    or staged_path.stem
                )
                app_id = self._unique_app_id(preliminary_name)
                installed_path = fmt.install_file(
                    staged_path,
                    self.config.apps_dir,
                    app_id,
                    in_place=False,
                )
                metadata = fmt.inspect(installed_path)
                app_name = custom_name.strip() or metadata.name or preliminary_name
            else:
                metadata = fmt.inspect(staged_path)
                app_name = (
                    custom_name.strip()
                    or metadata.name
                    or release.name_hint
                    or staged_path.stem
                )
                app_id = self._unique_app_id(app_name)
                installed_path = fmt.install_file(
                    staged_path,
                    self.config.apps_dir,
                    app_id,
                    in_place=in_place,
                )

            version = release.version
            if version in ("", "local", "unknown") and metadata.version_hint:
                version = normalize_version(metadata.version_hint)

            exec_path = ""
            if fmt.format_id == "archive" and metadata.exec_relpath:
                exec_candidate = installed_path / metadata.exec_relpath
                if exec_candidate.is_file():
                    exec_path = str(exec_candidate)

            now = _utc_now()
            record = AppRecord(
                id=app_id,
                name=app_name,
                format=fmt.format_id,
                provider=provider.provider_id,
                source_url=source.strip(),
                asset_pattern=asset_pattern.strip(),
                installed_version=version,
                install_path=str(installed_path),
                exec_path=exec_path,
                checksum_algo=checksum_algo,
                checksum_value=checksum_value,
                checksum_verified=checksum_verified,
                etag=etag,
                last_modified=last_modified,
                release_url=release.release_url,
                release_notes=release.release_notes,
                installed_at=now,
                updated_at=now,
                last_checked_at=now,
            )

            desktop_path, icon_path = integrate_app(
                record,
                metadata,
                self.paths,
                refresh_cache=self.refresh_desktop,
                icons_dir=self.icons_dir,
            )
            record.desktop_file = str(desktop_path)
            record.icon_path = str(icon_path) if icon_path else ""
            self.db.upsert_app(record)
            return record
        finally:
            if cleanup_staged:
                staged_path.unlink(missing_ok=True)

    def scan_unmanaged(self) -> list[Path]:
        """Return unmanaged AppImages and portable app folders inside apps_dir."""
        apps_dir = self.config.apps_dir.expanduser()
        if not apps_dir.is_dir():
            return []

        managed_paths: set[Path] = set()
        for app in self.db.list_apps():
            try:
                managed_paths.add(Path(app.install_path).expanduser().resolve())
            except OSError:
                pass

        unmanaged: list[Path] = []
        for candidate in sorted(apps_dir.iterdir()):
            if candidate.name.startswith("."):
                continue
            if candidate.resolve() in managed_paths:
                continue
            if candidate.is_file() and candidate.suffix.lower() == ".appimage":
                unmanaged.append(candidate)
            elif candidate.is_dir() and ArchiveFormat.looks_like_portable_app_dir(
                candidate
            ):
                unmanaged.append(candidate)
        return unmanaged

    def inspect_unmanaged(self, path: Path | str) -> UnmanagedPreview:
        """Safely inspect an unmanaged AppImage or portable folder before adopting it."""
        target = Path(path).expanduser().resolve()
        if not target.exists():
            raise AppimaError(f"Path no longer exists: {target}")

        fmt = self._resolve_format(target)
        metadata = fmt.inspect(target)
        version = normalize_version(metadata.version_hint) if metadata.version_hint else "local"

        exec_path = str(target)
        if fmt.format_id == "archive" and metadata.exec_relpath:
            cand = target / metadata.exec_relpath
            if cand.is_file():
                exec_path = str(cand)

        size_bytes = target.stat().st_size if target.is_file() else None
        conflicts = self.find_conflicting_apps(metadata.name)

        return UnmanagedPreview(
            path=target,
            format_id=fmt.format_id,
            name=metadata.name,
            version=version,
            exec_path=exec_path,
            size_bytes=size_bytes,
            icon_bytes=metadata.icon_bytes,
            icon_ext=metadata.icon_ext,
            conflicts=conflicts,
        )

    def check_update(self, app_id: str) -> UpdateCheckResult:
        """Check upstream for a single managed app and persist any discovered update."""
        record = self.db.get_app(app_id)
        if not record:
            raise AppimaError(f"App not found: {app_id}")

        if record.provider == "local" or not record.source_url:
            return UpdateCheckResult(
                app_id=record.id,
                app_name=record.name,
                installed_version=record.installed_version,
                update_available=False,
            )

        provider = self._provider_by_id(record.provider)
        if not provider:
            return UpdateCheckResult(
                app_id=record.id,
                app_name=record.name,
                installed_version=record.installed_version,
                error=f"Unknown provider: {record.provider}",
            )

        try:
            release = provider.resolve_latest(
                record.source_url,
                asset_pattern=record.asset_pattern,
                current=record,
            )
        except AppimaError as exc:
            return UpdateCheckResult(
                app_id=record.id,
                app_name=record.name,
                installed_version=record.installed_version,
                error=str(exc),
            )

        has_newer = is_newer_version(record.installed_version, release.version)
        # Also detect direct-URL changes when ETag/Last-Modified changed
        if (
            not has_newer
            and record.provider == "direct_url"
            and (
                (release.etag and record.etag and release.etag != record.etag)
                or (
                    release.last_modified
                    and record.last_modified
                    and release.last_modified != record.last_modified
                )
            )
        ):
            has_newer = True

        record.last_checked_at = _utc_now()
        if has_newer:
            record.available_version = release.version
            record.available_download_url = release.download_url
            record.available_checksum = release.checksum
            record.available_checksum_algo = release.checksum_algo
            if release.release_url:
                record.release_url = release.release_url
            if release.release_notes:
                record.release_notes = release.release_notes
        else:
            record.available_version = ""
            record.available_download_url = ""
            record.available_checksum = ""
            record.available_checksum_algo = ""

        self.db.upsert_app(record)
        return UpdateCheckResult(
            app_id=record.id,
            app_name=record.name,
            installed_version=record.installed_version,
            available_version=record.available_version,
            update_available=has_newer,
        )

    def check_all_updates(self) -> list[UpdateCheckResult]:
        """Check all managed apps for updates."""
        results: list[UpdateCheckResult] = []
        for app in self.db.list_apps():
            results.append(self.check_update(app.id))
        return results

    def update_app(
        self,
        app_id: str,
        expected_checksum: str = "",
        progress_cb: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> AppRecord:
        """Download and apply the latest version in place, backing up the previous version."""
        record = self.db.get_app(app_id)
        if not record:
            raise AppimaError(f"App not found: {app_id}")
        if record.provider == "local":
            raise AppimaError(
                f"{record.name} was installed from a local file and has no remote update source."
            )

        provider = self._provider_by_id(record.provider)
        if not provider:
            raise AppimaError(f"Unknown provider: {record.provider}")

        release = provider.resolve_latest(
            record.source_url,
            asset_pattern=record.asset_pattern,
            current=record,
        )

        dl = download_file(
            release.download_url,
            self.paths.staging_dir,
            filename=release.filename,
            progress_cb=progress_cb,
            cancel_event=cancel_event,
        )
        staged_path = dl.path

        try:
            checksum_input = expected_checksum.strip()
            if not checksum_input and release.checksum:
                checksum_input = (
                    f"{release.checksum_algo}:{release.checksum}"
                    if release.checksum_algo
                    else release.checksum
                )

            checksum_algo = "sha256"
            checksum_verified = False
            if checksum_input:
                ok, actual, expected, algo = verify_file_hash(staged_path, checksum_input)
                if not ok:
                    raise ChecksumMismatchError(expected, actual, algo)
                checksum_algo = algo
                checksum_value = actual
                checksum_verified = True
            else:
                checksum_value = compute_file_hash(staged_path, "sha256")

            fmt = self._format_by_id(record.format)
            current_path = Path(record.install_path)
            apps_dir = self.config.apps_dir.expanduser().resolve()
            apps_dir.mkdir(parents=True, exist_ok=True)

            if record.format == "archive":
                # Back up existing directory before replacing
                if self.config.keep_backup and current_path.is_dir():
                    self.paths.backups_dir.mkdir(parents=True, exist_ok=True)
                    if record.backup_path:
                        bp = Path(record.backup_path)
                        if bp.is_dir():
                            shutil.rmtree(bp, ignore_errors=True)
                        else:
                            bp.unlink(missing_ok=True)
                    safe_ver = slugify_app_id(record.installed_version or "prev")
                    backup_dir = self.paths.backups_dir / f"{record.id}-{safe_ver}"
                    if backup_dir.exists():
                        shutil.rmtree(backup_dir)
                    shutil.copytree(current_path, backup_dir, symlinks=True)
                    record.backup_path = str(backup_dir)
                    record.backup_version = record.installed_version

                target_path = fmt.install_file(
                    staged_path, apps_dir, record.id, in_place=False
                )
                try:
                    if current_path.resolve() != target_path.resolve() and current_path.is_dir():
                        shutil.rmtree(current_path, ignore_errors=True)
                except OSError:
                    pass
                metadata = fmt.inspect(target_path)
                if metadata.exec_relpath and (target_path / metadata.exec_relpath).is_file():
                    record.exec_path = str(target_path / metadata.exec_relpath)
            else:
                metadata = fmt.inspect(staged_path)

                # Back up current installed binary before replacing
                if self.config.keep_backup and current_path.is_file():
                    self.paths.backups_dir.mkdir(parents=True, exist_ok=True)
                    if record.backup_path:
                        Path(record.backup_path).unlink(missing_ok=True)
                    safe_ver = slugify_app_id(record.installed_version or "prev")
                    backup_file = self.paths.backups_dir / f"{record.id}-{safe_ver}.AppImage"
                    shutil.copy2(current_path, backup_file)
                    record.backup_path = str(backup_file)
                    record.backup_version = record.installed_version

                # Swap binary cleanly into canonical <apps_dir>/<id>.AppImage
                canonical_path = apps_dir / f"{record.id}.AppImage"
                try:
                    in_apps_dir = current_path.expanduser().resolve().parent == apps_dir
                except OSError:
                    in_apps_dir = False

                target_path = canonical_path if in_apps_dir else current_path
                target_dir = target_path.parent
                target_dir.mkdir(parents=True, exist_ok=True)
                tmp_target = target_dir / f".{target_path.name}.updating"
                shutil.copy2(staged_path, tmp_target)
                tmp_target.chmod(0o755)
                tmp_target.replace(target_path)

                try:
                    if current_path.resolve() != target_path.resolve() and current_path.is_file():
                        current_path.unlink(missing_ok=True)
                except OSError:
                    pass

                if record.id == SELF_APP_ID:
                    self._cleanup_stale_self_appimages(apps_dir, keep_path=target_path)

            now = _utc_now()
            record.install_path = str(target_path)
            record.installed_version = release.version
            record.available_version = ""
            record.available_download_url = ""
            record.available_checksum = ""
            record.available_checksum_algo = ""
            record.checksum_algo = checksum_algo
            record.checksum_value = checksum_value
            record.checksum_verified = checksum_verified
            record.etag = dl.etag or release.etag
            record.last_modified = dl.last_modified or release.last_modified
            record.release_url = release.release_url or record.release_url
            record.release_notes = release.release_notes or record.release_notes
            record.updated_at = now
            record.last_checked_at = now

            desktop_path, icon_path = integrate_app(
                record,
                metadata,
                self.paths,
                refresh_cache=self.refresh_desktop,
                icons_dir=self.icons_dir,
            )
            record.desktop_file = str(desktop_path)
            if icon_path:
                record.icon_path = str(icon_path)

            self.db.upsert_app(record)
            return record
        finally:
            staged_path.unlink(missing_ok=True)

    def rollback_app(self, app_id: str) -> AppRecord:
        """Restore the backed-up previous version of an app."""
        record = self.db.get_app(app_id)
        if not record:
            raise AppimaError(f"App not found: {app_id}")
        if not record.has_backup:
            raise AppimaError(f"No backup available to roll back {record.name}.")

        backup_path = Path(record.backup_path)
        install_path = Path(record.install_path)
        install_path.parent.mkdir(parents=True, exist_ok=True)

        if backup_path.is_dir():
            tmp_target = install_path.parent / f".{install_path.name}.rollback"
            if tmp_target.exists():
                shutil.rmtree(tmp_target)
            shutil.copytree(backup_path, tmp_target, symlinks=True)
            if install_path.is_dir():
                shutil.rmtree(install_path)
            else:
                install_path.unlink(missing_ok=True)
            tmp_target.replace(install_path)
            shutil.rmtree(backup_path, ignore_errors=True)
            record.checksum_value = ""
        else:
            tmp_target = install_path.parent / f".{install_path.name}.rollback"
            shutil.copy2(backup_path, tmp_target)
            tmp_target.chmod(0o755)
            tmp_target.replace(install_path)
            backup_path.unlink(missing_ok=True)
            record.checksum_algo = "sha256"
            record.checksum_value = compute_file_hash(install_path, "sha256")

        record.installed_version = record.backup_version or "previous"
        record.backup_path = ""
        record.backup_version = ""
        record.checksum_verified = False
        record.updated_at = _utc_now()

        fmt = self._format_by_id(record.format)
        metadata = fmt.inspect(install_path)
        if record.format == "archive" and metadata.exec_relpath:
            cand = install_path / metadata.exec_relpath
            if cand.is_file():
                record.exec_path = str(cand)
        desktop_path, icon_path = integrate_app(
            record,
            metadata,
            self.paths,
            refresh_cache=self.refresh_desktop,
            icons_dir=self.icons_dir,
        )
        record.desktop_file = str(desktop_path)
        if icon_path:
            record.icon_path = str(icon_path)

        self.db.upsert_app(record)
        return record

    def update_app_settings(
        self,
        app_id: str,
        name: str | None = None,
        launch_args: str | None = None,
        env_vars: str | None = None,
        source_url: str | None = None,
        asset_pattern: str | None = None,
        exec_path: str | None = None,
        custom_icon_file: Path | str | None = None,
        reextract_icon: bool = False,
    ) -> AppRecord:
        """Update per-app metadata/launch options/icon and refresh its .desktop entry."""
        record = self.db.get_app(app_id)
        if not record:
            raise AppimaError(f"App not found: {app_id}")

        if name is not None and name.strip():
            record.name = name.strip()
        if launch_args is not None:
            record.launch_args = launch_args.strip()
        if env_vars is not None:
            record.env_vars = env_vars.strip()
        if exec_path is not None and record.format == "archive":
            cleaned_exec = exec_path.strip()
            if cleaned_exec:
                exec_file = Path(cleaned_exec).expanduser()
                if not exec_file.is_file():
                    raise AppimaError(f"Executable not found: {exec_file}")
                record.exec_path = str(exec_file)
        if source_url is not None:
            cleaned_url = source_url.strip()
            record.source_url = cleaned_url
            if cleaned_url:
                try:
                    record.provider = self._resolve_provider(cleaned_url).provider_id
                except ProviderError:
                    pass
        if asset_pattern is not None:
            record.asset_pattern = asset_pattern.strip()

        return self.reintegrate_app(
            record,
            custom_icon_file=custom_icon_file,
            reextract_icon=reextract_icon,
        )

    def reintegrate_app(
        self,
        app_or_id: AppRecord | str,
        custom_icon_file: Path | str | None = None,
        reextract_icon: bool = False,
    ) -> AppRecord:
        """Re-extract metadata and rewrite the managed .desktop entry."""
        record = (
            self.db.get_app(app_or_id) if isinstance(app_or_id, str) else app_or_id
        )
        if not record:
            raise AppimaError(f"App not found: {app_or_id}")

        install_path = Path(record.install_path)
        fmt = self._format_by_id(record.format)
        metadata = fmt.inspect(install_path)

        if custom_icon_file:
            icon_src = Path(custom_icon_file).expanduser()
            if not icon_src.is_file():
                raise AppimaError(f"Icon file not found: {icon_src}")
            ext = icon_src.suffix.lower().lstrip(".")
            if ext not in ("png", "svg", "xpm", "ico", "webp"):
                raise AppimaError(
                    f"Unsupported icon format '.{ext}'. Use PNG, SVG, XPM, ICO, or WebP."
                )
            metadata.icon_bytes = icon_src.read_bytes()
            metadata.icon_ext = ext
        elif not reextract_icon and record.icon_path and Path(record.icon_path).is_file():
            # Preserve an existing (possibly user-customized) icon unless re-extraction was requested
            metadata.icon_bytes = None
            metadata.icon_ext = ""

        desktop_path, icon_path = integrate_app(
            record,
            metadata,
            self.paths,
            refresh_cache=self.refresh_desktop,
            icons_dir=self.icons_dir,
        )
        record.desktop_file = str(desktop_path)
        if icon_path:
            record.icon_path = str(icon_path)
        self.db.upsert_app(record)
        return record

    def uninstall_app(self, app_id: str) -> None:
        """Cleanly remove the app binary, backup, .desktop file, icon, and DB entry."""
        record = self.db.get_app(app_id)
        if not record:
            raise AppimaError(f"App not found: {app_id}")

        remove_integration(
            record,
            self.paths,
            refresh_cache=self.refresh_desktop,
            icons_dir=self.icons_dir,
        )

        install_path = Path(record.install_path)
        try:
            fmt = self._format_by_id(record.format)
            fmt.remove_installed(install_path)
        except FormatError:
            install_path.unlink(missing_ok=True)

        if record.backup_path:
            bp = Path(record.backup_path)
            if bp.is_dir():
                shutil.rmtree(bp, ignore_errors=True)
            else:
                bp.unlink(missing_ok=True)

        self.db.delete_app(app_id)

    def needs_self_integration(self, appimage_path: Path | None = None) -> bool:
        """Return True if running as an AppImage that is not yet integrated into apps_dir."""
        running = appimage_path or current_appimage_path()
        if not running or not running.is_file():
            return False

        existing = self.db.get_app(SELF_APP_ID)
        if existing and existing.is_present and existing.desktop_file:
            if Path(existing.desktop_file).is_file():
                return False
        return True

    def integrate_self(
        self,
        appimage_path: Path | None = None,
        source_url: str = SELF_GITHUB_REPO,
    ) -> AppRecord:
        """Install/adopt the running appima AppImage into apps_dir with desktop & update integration."""
        running = (appimage_path or current_appimage_path())
        if not running or not running.is_file():
            raise AppimaError(
                "Self-integration is only available when running appima as an AppImage."
            )

        running = running.resolve()
        apps_dir = self.config.apps_dir.expanduser().resolve()
        apps_dir.mkdir(parents=True, exist_ok=True)
        canonical_path = apps_dir / f"{SELF_APP_ID}.AppImage"

        fmt = self._format_by_id("appimage")
        if running == canonical_path:
            installed_path = fmt.install_file(
                running,
                apps_dir,
                SELF_APP_ID,
                in_place=True,
            )
        else:
            installed_path = fmt.install_file(
                running,
                apps_dir,
                SELF_APP_ID,
                in_place=False,
            )
            # If the user launched a versioned appima-*.AppImage already inside apps_dir,
            # remove that old duplicate so apps_dir stays clean with a single appima.AppImage.
            if running.parent == apps_dir:
                running.unlink(missing_ok=True)

        self._cleanup_stale_self_appimages(apps_dir, keep_path=installed_path)

        # Prefer appima's own SVG icon asset if SquashFS extraction didn't yield one
        metadata = fmt.inspect(installed_path)
        if not metadata.icon_bytes:
            asset_icon = gui_asset_path("appima.svg")
            if asset_icon.is_file():
                metadata.icon_bytes = asset_icon.read_bytes()
                metadata.icon_ext = "svg"
        if not metadata.name or metadata.name.lower().startswith("appima"):
            metadata = ExtractedMetadata(
                name="appima",
                comment="Minimalist AppImage & Portable App Manager",
                categories="System;Settings;Utility;",
                icon_bytes=metadata.icon_bytes,
                icon_ext=metadata.icon_ext,
                desktop_content=metadata.desktop_content,
                wm_class="appima",
            )

        now = _utc_now()
        existing = self.db.get_app(SELF_APP_ID)
        record = AppRecord(
            id=SELF_APP_ID,
            name="appima",
            format="appimage",
            provider="github",
            source_url=source_url,
            installed_version=__version__,
            install_path=str(installed_path),
            checksum_algo="sha256",
            checksum_value=compute_file_hash(installed_path, "sha256"),
            checksum_verified=False,
            launch_args=existing.launch_args if existing else "",
            env_vars=existing.env_vars if existing else "",
            release_url=source_url,
            installed_at=existing.installed_at if existing else now,
            updated_at=now,
            last_checked_at=existing.last_checked_at if existing else now,
        )

        desktop_path, icon_path = integrate_app(
            record,
            metadata,
            self.paths,
            refresh_cache=self.refresh_desktop,
            icons_dir=self.icons_dir,
        )
        record.desktop_file = str(desktop_path)
        record.icon_path = str(icon_path) if icon_path else ""
        self.db.upsert_app(record)
        return record

    @staticmethod
    def _cleanup_stale_self_appimages(apps_dir: Path, keep_path: Path) -> None:
        """Remove stale versioned appima-*.AppImage files left inside apps_dir."""
        if not apps_dir.is_dir():
            return
        keep_resolved = keep_path.resolve()
        for candidate in apps_dir.iterdir():
            if not candidate.is_file():
                continue
            low = candidate.name.lower()
            if low.startswith("appima-") and low.endswith(".appimage"):
                try:
                    if candidate.resolve() != keep_resolved:
                        candidate.unlink(missing_ok=True)
                except OSError:
                    pass

    def _unique_app_id(self, name: str) -> str:
        base = slugify_app_id(name)
        candidate = base
        counter = 2
        while self.db.get_app(candidate) is not None:
            candidate = f"{base}-{counter}"
            counter += 1
        return candidate
