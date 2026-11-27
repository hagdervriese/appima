"""High-level AppManager orchestrating install, integration, updates, and rollback."""

from __future__ import annotations

import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from appima.core.checksum import compute_file_hash, verify_file_hash
from appima.core.config import AppimaPaths, load_config, save_config
from appima.core.database import Database
from appima.core.desktop import integrate_app, remove_integration
from appima.core.downloader import ProgressCallback, download_file
from appima.core.models import (
    AppimaError,
    AppRecord,
    ChecksumMismatchError,
    Config,
    FormatError,
    ProviderError,
    ReleaseInfo,
    UpdateCheckResult,
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
        return provider.resolve_latest(source, asset_pattern=asset_pattern)

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

            fmt = self._resolve_format(staged_path)
            metadata = fmt.inspect(staged_path)

            app_name = (
                custom_name.strip()
                or metadata.name
                or release.name_hint
                or staged_path.stem
            )
            app_id = self._unique_app_id(app_name)

            version = release.version
            if version in ("", "local", "unknown") and metadata.version_hint:
                version = normalize_version(metadata.version_hint)

            installed_path = fmt.install_file(
                staged_path,
                self.config.apps_dir,
                app_id,
                in_place=in_place,
            )

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
                record, metadata, self.paths, refresh_cache=self.refresh_desktop
            )
            record.desktop_file = str(desktop_path)
            record.icon_path = str(icon_path) if icon_path else ""
            self.db.upsert_app(record)
            return record
        finally:
            if cleanup_staged:
                staged_path.unlink(missing_ok=True)

    def scan_unmanaged(self) -> list[Path]:
        """Return .AppImage files inside apps_dir that are not yet tracked by appima."""
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
            if not candidate.is_file() or candidate.name.startswith("."):
                continue
            if candidate.suffix.lower() != ".appimage":
                continue
            if candidate.resolve() not in managed_paths:
                unmanaged.append(candidate)
        return unmanaged

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
            metadata = fmt.inspect(staged_path)

            # Back up current installed binary before replacing
            current_path = Path(record.install_path)
            if self.config.keep_backup and current_path.is_file():
                self.paths.backups_dir.mkdir(parents=True, exist_ok=True)
                # Remove older backup for this app
                if record.backup_path:
                    Path(record.backup_path).unlink(missing_ok=True)
                safe_ver = slugify_app_id(record.installed_version or "prev")
                backup_file = self.paths.backups_dir / f"{record.id}-{safe_ver}.AppImage"
                shutil.copy2(current_path, backup_file)
                record.backup_path = str(backup_file)
                record.backup_version = record.installed_version

            # Swap binary at the exact existing install_path so pinned icons stay valid
            target_dir = current_path.parent
            target_dir.mkdir(parents=True, exist_ok=True)
            tmp_target = target_dir / f".{current_path.name}.updating"
            shutil.copy2(staged_path, tmp_target)
            tmp_target.chmod(0o755)
            tmp_target.replace(current_path)

            now = _utc_now()
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
                record, metadata, self.paths, refresh_cache=self.refresh_desktop
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

        tmp_target = install_path.parent / f".{install_path.name}.rollback"
        shutil.copy2(backup_path, tmp_target)
        tmp_target.chmod(0o755)
        tmp_target.replace(install_path)
        backup_path.unlink(missing_ok=True)

        record.installed_version = record.backup_version or "previous"
        record.backup_path = ""
        record.backup_version = ""
        record.checksum_algo = "sha256"
        record.checksum_value = compute_file_hash(install_path, "sha256")
        record.checksum_verified = False
        record.updated_at = _utc_now()

        fmt = self._format_by_id(record.format)
        metadata = fmt.inspect(install_path)
        desktop_path, icon_path = integrate_app(
            record, metadata, self.paths, refresh_cache=self.refresh_desktop
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
    ) -> AppRecord:
        """Update per-app metadata/launch options and refresh its .desktop entry."""
        record = self.db.get_app(app_id)
        if not record:
            raise AppimaError(f"App not found: {app_id}")

        if name is not None and name.strip():
            record.name = name.strip()
        if launch_args is not None:
            record.launch_args = launch_args.strip()
        if env_vars is not None:
            record.env_vars = env_vars.strip()
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

        return self.reintegrate_app(record)

    def reintegrate_app(self, app_or_id: AppRecord | str) -> AppRecord:
        """Re-extract metadata and rewrite the managed .desktop entry."""
        record = (
            self.db.get_app(app_or_id) if isinstance(app_or_id, str) else app_or_id
        )
        if not record:
            raise AppimaError(f"App not found: {app_or_id}")

        install_path = Path(record.install_path)
        fmt = self._format_by_id(record.format)
        metadata = fmt.inspect(install_path)
        desktop_path, icon_path = integrate_app(
            record, metadata, self.paths, refresh_cache=self.refresh_desktop
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

        remove_integration(record, self.paths, refresh_cache=self.refresh_desktop)

        install_path = Path(record.install_path)
        try:
            fmt = self._format_by_id(record.format)
            fmt.remove_installed(install_path)
        except FormatError:
            install_path.unlink(missing_ok=True)

        if record.backup_path:
            Path(record.backup_path).unlink(missing_ok=True)

        self.db.delete_app(app_id)

    def _unique_app_id(self, name: str) -> str:
        base = slugify_app_id(name)
        candidate = base
        counter = 2
        while self.db.get_app(candidate) is not None:
            candidate = f"{base}-{counter}"
            counter += 1
        return candidate
