from __future__ import annotations

from datetime import UTC, datetime

import pytest
from dgx_control.jobs import JobService
from dgx_control.models import Base
from dgx_control.settings import Settings, SettingsError, WorkerSettings
from dgx_control.worker import Worker
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _jobs(tmp_path) -> JobService:
    engine = create_engine(f"sqlite:///{tmp_path / 'production-worker.sqlite'}")
    Base.metadata.create_all(engine)
    return JobService(
        sessionmaker(engine, expire_on_commit=False),
        clock=lambda: datetime(2026, 8, 5, tzinfo=UTC),
    )


def test_production_worker_has_no_automatic_direct_transport_fallback(
    tmp_path,
) -> None:
    jobs = _jobs(tmp_path)
    job = jobs.enqueue("probe", "operator", "a" * 40, [], {})

    assert Worker(
        jobs,
        "worker",
        {},
        reconciliations=None,
        quarantine_unlinked=True,
    ).run_once() is True
    persisted = jobs.get(job.id)
    assert persisted.state == "waiting-for-operator"
    assert persisted.status_reason == "legacy unlinked job requires operator review"
    assert persisted.current_attempt == 0


def test_production_worker_settings_load_only_worker_authority_secrets(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "database-url"
    token = tmp_path / "worker-api-token"
    database.write_text("postgresql://control:test@postgres/control")
    token.write_text("w" * 32)
    monkeypatch.setenv("DGX_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("DGX_DATABASE_URL_FILE", str(database))
    monkeypatch.setenv("DGX_WORKER_API_TOKEN_FILE", str(token))
    monkeypatch.setenv("DGX_MANAGEMENT_CIDRS", "10.0.0.0/24")
    monkeypatch.setenv("DGX_INTERNAL_API_URL", "http://control-api:8000")

    settings = WorkerSettings.from_env_and_secrets()

    assert settings.database_url == database.read_text()
    assert settings.internal_api_token == b"w" * 32
    assert settings.internal_api_url == "http://control-api:8000"
    for forbidden in (
        "repository_path",
        "git_signing_key_path",
        "token_signing_key",
        "metrics_token",
        "agent_ca_credential_path",
    ):
        assert not hasattr(settings, forbidden)


def test_production_worker_settings_reject_raw_or_cross_origin_authority(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "database-url"
    database.write_text("postgresql://control:test@postgres/control")
    monkeypatch.setenv("DGX_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("DGX_DATABASE_URL_FILE", str(database))
    monkeypatch.setenv("DGX_WORKER_API_TOKEN", "w" * 32)
    monkeypatch.setenv("DGX_MANAGEMENT_CIDRS", "10.0.0.0/24")
    monkeypatch.setenv("DGX_INTERNAL_API_URL", "http://127.0.0.1:8000/path")

    with pytest.raises(SettingsError):
        WorkerSettings.from_env_and_secrets()

    token = tmp_path / "worker-api-token"
    token.write_text("w" * 32)
    monkeypatch.delenv("DGX_WORKER_API_TOKEN")
    monkeypatch.setenv("DGX_WORKER_API_TOKEN_FILE", str(token))
    with pytest.raises(SettingsError, match="fixed HTTP origin"):
        WorkerSettings.from_env_and_secrets()


def test_legacy_direct_transport_defaults_disabled(monkeypatch) -> None:
    monkeypatch.setenv("DGX_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv(
        "DGX_DATABASE_URL",
        "postgresql://control:test@postgres/control",
    )
    monkeypatch.delenv("DGX_LEGACY_DIRECT_TRANSPORT", raising=False)

    assert Settings.from_env_and_secrets().legacy_direct_transport == ""


def test_only_exact_test_selector_can_authorize_legacy_transport(monkeypatch) -> None:
    monkeypatch.setenv("DGX_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv(
        "DGX_DATABASE_URL",
        "postgresql://control:test@postgres/control",
    )
    monkeypatch.setenv(
        "DGX_LEGACY_DIRECT_TRANSPORT",
        "explicit-test-only",
    )

    assert (
        Settings.from_env_and_secrets().legacy_direct_transport
        == "explicit-test-only"
    )

    monkeypatch.setenv("DGX_LEGACY_DIRECT_TRANSPORT", "enabled")
    with pytest.raises(SettingsError, match="legacy direct transport"):
        Settings.from_env_and_secrets()


def test_production_rejects_legacy_selector_before_loading_other_secrets(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DGX_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv(
        "DGX_LEGACY_DIRECT_TRANSPORT",
        "explicit-test-only",
    )

    with pytest.raises(SettingsError, match="forbidden in production"):
        Settings.from_env_and_secrets()
