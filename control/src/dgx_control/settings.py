"""Strict application configuration loaded from paths and secret files."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class SettingsError(ValueError):
    pass


def _secret(name: str, *, production: bool) -> str:
    raw_name = name.removesuffix("_FILE")
    raw = os.environ.get(raw_name)
    source = os.environ.get(name)
    if production and raw:
        raise SettingsError(f"{raw_name} must be supplied through a secret file")
    if source:
        path = Path(source)
        if path.is_symlink() or not path.is_file():
            raise SettingsError(f"{name} must name a regular non-symlink file")
        value = path.read_text().strip()
    else:
        value = raw or ""
    if not value:
        raise SettingsError(f"{name} is required")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str
    repository_path: Path
    state_path: Path
    deployment_mode: str

    @property
    def database_host(self) -> str | None:
        return urlsplit(self.database_url).hostname

    @classmethod
    def from_env_and_secrets(cls) -> "Settings":
        mode = os.environ.get("DGX_DEPLOYMENT_MODE", "development")
        if mode not in {"development", "test", "production"}:
            raise SettingsError("DGX_DEPLOYMENT_MODE is invalid")
        database_url = _secret("DGX_DATABASE_URL_FILE", production=mode == "production")
        if urlsplit(database_url).scheme not in {"postgresql", "postgresql+psycopg"}:
            raise SettingsError("database URL must use PostgreSQL")
        return cls(
            database_url=database_url,
            repository_path=Path(os.environ.get("DGX_REPOSITORY_PATH", "/srv/dgx-forge/repository")),
            state_path=Path(os.environ.get("DGX_STATE_PATH", "/srv/dgx-forge/state")),
            deployment_mode=mode,
        )
