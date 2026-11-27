"""XDG-compliant paths and configuration persistence for appima."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from appima.core.models import Config


@dataclass(frozen=True)
class AppimaPaths:
    """All filesystem locations used by appima (overrideable for testing)."""

    config_dir: Path
    data_dir: Path
    cache_dir: Path
    applications_dir: Path
    systemd_user_dir: Path

    @classmethod
    def default(cls) -> "AppimaPaths":
        home = Path.home()
        xdg_config = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
        xdg_data = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local" / "share"))
        xdg_cache = Path(os.environ.get("XDG_CACHE_HOME") or (home / ".cache"))
        return cls(
            config_dir=xdg_config / "appima",
            data_dir=xdg_data / "appima",
            cache_dir=xdg_cache / "appima",
            applications_dir=xdg_data / "applications",
            systemd_user_dir=xdg_config / "systemd" / "user",
        )

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def db_file(self) -> Path:
        return self.data_dir / "appima.db"

    @property
    def icons_dir(self) -> Path:
        return self.data_dir / "icons"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def staging_dir(self) -> Path:
        return self.cache_dir / "staging"

    def ensure_dirs(self) -> None:
        for directory in (
            self.config_dir,
            self.data_dir,
            self.cache_dir,
            self.icons_dir,
            self.backups_dir,
            self.staging_dir,
            self.applications_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


def load_config(paths: AppimaPaths) -> Config:
    """Load config from disk, returning defaults if none exists yet."""
    if not paths.config_file.exists():
        return Config()
    try:
        data = json.loads(paths.config_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return Config.from_dict(data)
    except (OSError, json.JSONDecodeError):
        pass
    return Config()


def save_config(config: Config, paths: AppimaPaths) -> None:
    """Atomically write config to disk."""
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = paths.config_file.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(config.to_dict(), indent=2) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(paths.config_file)
