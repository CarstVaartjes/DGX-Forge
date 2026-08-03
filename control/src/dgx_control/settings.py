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


def _secret_path(name: str) -> Path:
    source = os.environ.get(name)
    if not source:
        raise SettingsError(f"{name} is required")
    path = Path(source)
    if path.is_symlink() or not path.is_file():
        raise SettingsError(f"{name} must name a regular non-symlink file")
    return path


@dataclass(frozen=True)
class Settings:
    database_url: str
    repository_path: Path
    state_path: Path
    deployment_mode: str
    token_signing_key: bytes
    metrics_token: str
    git_signing_key_path: Path | None
    deployment_branch: str
    required_checks: tuple[str, ...]
    agent_ca_provider: str
    agent_runtime: str
    agent_client_ca: bytes
    agent_intermediate_certificate: bytes
    agent_intermediate_key_path: Path | None
    agent_ca_credential: bytes
    agent_proxy_auth: bytes

    @property
    def database_host(self) -> str | None:
        return urlsplit(self.database_url).hostname

    @classmethod
    def from_env_and_secrets(cls) -> "Settings":
        mode = os.environ.get("DGX_DEPLOYMENT_MODE", "development")
        if mode not in {"development", "test", "production"}:
            raise SettingsError("DGX_DEPLOYMENT_MODE is invalid")
        agent_ca_provider = os.environ.get("DGX_AGENT_CA_PROVIDER", "")
        agent_runtime = os.environ.get("DGX_AGENT_RUNTIME", "enabled")
        if agent_runtime not in {"enabled", "disabled"}:
            raise SettingsError("DGX_AGENT_RUNTIME is invalid")
        builtin_bootstrap = os.environ.get("DGX_AGENT_BUILTIN_CA_BOOTSTRAP", "")
        if builtin_bootstrap not in {"", "1"}:
            raise SettingsError("DGX_AGENT_BUILTIN_CA_BOOTSTRAP is invalid")
        if agent_ca_provider == "builtin" and builtin_bootstrap != "1":
            raise SettingsError("built-in CA requires explicit bootstrap selection")
        if agent_ca_provider != "builtin" and builtin_bootstrap:
            raise SettingsError("built-in CA bootstrap requires the builtin provider")
        if mode == "production" and not agent_ca_provider:
            raise SettingsError("DGX_AGENT_CA_PROVIDER is required in production")
        if agent_ca_provider and agent_ca_provider not in {"step-ca", "builtin"}:
            raise SettingsError("DGX_AGENT_CA_PROVIDER is invalid")
        database_url = _secret("DGX_DATABASE_URL_FILE", production=mode == "production")
        if urlsplit(database_url).scheme not in {"postgresql", "postgresql+psycopg"}:
            raise SettingsError("database URL must use PostgreSQL")
        signing_file = os.environ.get("DGX_TOKEN_SIGNING_KEY_FILE")
        if signing_file:
            signing_path = Path(signing_file)
            if signing_path.is_symlink() or not signing_path.is_file():
                raise SettingsError("token signing key must be a regular non-symlink file")
            signing_key = signing_path.read_bytes().strip()
        elif mode == "production":
            raise SettingsError("DGX_TOKEN_SIGNING_KEY_FILE is required in production")
        else:
            signing_key = b"development-only-signing-key-32b"
        if len(signing_key) < 32:
            raise SettingsError("token signing key must contain at least 32 bytes")
        metrics_file = os.environ.get("DGX_METRICS_TOKEN_FILE")
        if metrics_file:
            metrics_path = Path(metrics_file)
            if metrics_path.is_symlink() or not metrics_path.is_file():
                raise SettingsError("metrics token must be a regular non-symlink file")
            metrics_token = metrics_path.read_text().strip()
        elif mode == "production":
            raise SettingsError("DGX_METRICS_TOKEN_FILE is required in production")
        else:
            metrics_token = "development-metrics-token"
        if len(metrics_token) < 16 or any(character.isspace() for character in metrics_token):
            raise SettingsError("metrics token is invalid")
        git_signing_raw = os.environ.get("DGX_GIT_SIGNING_KEY_FILE")
        git_signing_key_path = Path(git_signing_raw) if git_signing_raw else None
        if git_signing_key_path is not None and (
            git_signing_key_path.is_symlink() or not git_signing_key_path.is_file()
        ):
            raise SettingsError("Git signing key must be a regular non-symlink file")
        if mode == "production" and git_signing_key_path is None:
            raise SettingsError("DGX_GIT_SIGNING_KEY_FILE is required in production")
        deployment_branch = os.environ.get("DGX_DEPLOYMENT_BRANCH", "deploy")
        if not deployment_branch or any(value in deployment_branch for value in ("..", "//", "\n", "\x00")):
            raise SettingsError("deployment branch is invalid")
        required_checks = tuple(
            value.strip() for value in os.environ.get("DGX_REQUIRED_CHECKS", "").split(",")
            if value.strip()
        )
        if len(required_checks) != len(set(required_checks)):
            raise SettingsError("required checks must be unique")
        agent_enabled = mode == "production" and agent_runtime == "enabled"
        agent_client_ca = _secret("DGX_AGENT_CLIENT_CA_FILE", production=True).encode() if agent_enabled else b""
        agent_intermediate_certificate = (
            _secret("DGX_AGENT_INTERMEDIATE_CERTIFICATE_FILE", production=True).encode()
            if agent_enabled else b""
        )
        agent_intermediate_key_path = (
            _secret_path("DGX_AGENT_INTERMEDIATE_KEY_FILE")
            if mode == "production" and agent_ca_provider == "builtin" else None
        )
        agent_ca_credential = (
            _secret("DGX_AGENT_CA_CREDENTIAL_FILE", production=True).encode()
            if agent_enabled and agent_ca_provider == "step-ca" else b""
        )
        agent_proxy_auth = _secret("DGX_AGENT_PROXY_AUTH_FILE", production=True).encode() if agent_enabled else b""
        if agent_proxy_auth and len(agent_proxy_auth) < 32:
            raise SettingsError("DGX_AGENT_PROXY_AUTH_FILE must contain at least 32 bytes")
        return cls(
            database_url=database_url,
            repository_path=Path(os.environ.get("DGX_REPOSITORY_PATH", "/srv/dgx-forge/repository")),
            state_path=Path(os.environ.get("DGX_STATE_PATH", "/srv/dgx-forge/state")),
            deployment_mode=mode,
            token_signing_key=signing_key,
            metrics_token=metrics_token,
            git_signing_key_path=git_signing_key_path,
            deployment_branch=deployment_branch,
            required_checks=required_checks,
            agent_ca_provider=agent_ca_provider,
            agent_runtime=agent_runtime,
            agent_client_ca=agent_client_ca,
            agent_intermediate_certificate=agent_intermediate_certificate,
            agent_intermediate_key_path=agent_intermediate_key_path,
            agent_ca_credential=agent_ca_credential,
            agent_proxy_auth=agent_proxy_auth,
        )
