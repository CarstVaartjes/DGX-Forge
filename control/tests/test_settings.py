from pathlib import Path

import pytest

from dgx_control.settings import Settings, SettingsError


def test_database_secret_is_read_from_file(tmp_path: Path, monkeypatch) -> None:
    secret = tmp_path / "database-url"
    secret.write_text("postgresql+psycopg://control:pw@postgres/control\n")
    monkeypatch.setenv("DGX_DATABASE_URL_FILE", str(secret))
    settings = Settings.from_env_and_secrets()
    assert settings.database_host == "postgres"
    assert settings.repository_path == Path("/srv/dgx-forge/repository")


def test_production_rejects_raw_database_secret(monkeypatch) -> None:
    monkeypatch.setenv("DGX_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("DGX_DATABASE_URL", "postgresql://unsafe")
    with pytest.raises(SettingsError, match="secret file"):
        Settings.from_env_and_secrets()


def test_secret_file_must_not_be_symlink(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "actual"
    target.write_text("postgresql://db/control")
    link = tmp_path / "database-url"
    link.symlink_to(target)
    monkeypatch.setenv("DGX_DATABASE_URL_FILE", str(link))
    with pytest.raises(SettingsError, match="regular non-symlink"):
        Settings.from_env_and_secrets()


def test_git_policy_configuration_uses_key_reference_and_unique_checks(tmp_path: Path, monkeypatch) -> None:
    key = tmp_path / "signing-key"
    key.write_text("fixture")
    monkeypatch.setenv("DGX_DATABASE_URL", "postgresql://db/control")
    monkeypatch.setenv("DGX_GIT_SIGNING_KEY_FILE", str(key))
    monkeypatch.setenv("DGX_DEPLOYMENT_BRANCH", "deploy")
    monkeypatch.setenv("DGX_REQUIRED_CHECKS", "tests,security")
    settings = Settings.from_env_and_secrets()
    assert settings.git_signing_key_path == key
    assert settings.required_checks == ("tests", "security")

    monkeypatch.setenv("DGX_REQUIRED_CHECKS", "tests,tests")
    with pytest.raises(SettingsError, match="unique"):
        Settings.from_env_and_secrets()


def test_compose_is_platform_neutral_and_only_caddy_publishes_ports() -> None:
    root = Path(__file__).resolve().parents[2]
    text = (root / "deploy/compose/compose.yaml").read_text()
    assert "ugreen" not in text.lower()
    assert "192.168." not in text
    assert "spark1" not in text.lower() and "spark2" not in text.lower()
    assert text.count("ports:") == 1
    assert "control-api:" in text and "control-worker:" in text
    assert "postgres:" in text and "caddy:" in text
