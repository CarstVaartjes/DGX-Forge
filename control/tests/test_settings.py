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
    monkeypatch.setenv("DGX_AGENT_CA_PROVIDER", "step-ca")
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


def test_production_agent_boundary_requires_secret_files_and_step_ca(tmp_path: Path, monkeypatch) -> None:
    values = {
        "DGX_DATABASE_URL_FILE": "postgresql://db/control",
        "DGX_TOKEN_SIGNING_KEY_FILE": "k" * 32,
        "DGX_METRICS_TOKEN_FILE": "m" * 16,
        "DGX_GIT_SIGNING_KEY_FILE": "git-key",
        "DGX_AGENT_CLIENT_CA_FILE": "client-ca",
        "DGX_AGENT_INTERMEDIATE_CERTIFICATE_FILE": "intermediate-certificate",
        "DGX_AGENT_CA_CREDENTIAL_FILE": "provider-credential",
        "DGX_AGENT_PROXY_AUTH_FILE": "p" * 32 + "\r\n",
    }
    monkeypatch.setenv("DGX_DEPLOYMENT_MODE", "production")
    for name, value in values.items():
        path = tmp_path / name
        path.write_text(value)
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv("DGX_AGENT_CA_PROVIDER", "step-ca")

    settings = Settings.from_env_and_secrets()

    assert settings.agent_ca_provider == "step-ca"
    assert settings.agent_proxy_auth == ("p" * 32).encode()


@pytest.mark.parametrize(
    "proxy_auth",
    (
        "p" * 31 + "\n",
        "p" * 32 + " ",
        " " + "p" * 32,
        "p" * 16 + " " + "p" * 16,
        "p" * 31 + "=",
        "p" * 16 + "\n" + "p" * 16,
        "p" * 16 + "\x00" + "p" * 16,
    ),
)
def test_production_rejects_noncanonical_agent_proxy_auth(
    tmp_path: Path,
    monkeypatch,
    proxy_auth: str,
) -> None:
    values = {
        "DGX_DATABASE_URL_FILE": "postgresql://db/control",
        "DGX_TOKEN_SIGNING_KEY_FILE": "k" * 32,
        "DGX_METRICS_TOKEN_FILE": "m" * 16,
        "DGX_GIT_SIGNING_KEY_FILE": "git-key",
        "DGX_AGENT_CLIENT_CA_FILE": "client-ca",
        "DGX_AGENT_INTERMEDIATE_CERTIFICATE_FILE": "intermediate-certificate",
        "DGX_AGENT_CA_CREDENTIAL_FILE": "provider-credential",
        "DGX_AGENT_PROXY_AUTH_FILE": proxy_auth,
    }
    monkeypatch.setenv("DGX_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("DGX_AGENT_CA_PROVIDER", "step-ca")
    for name, value in values.items():
        path = tmp_path / name
        path.write_text(value)
        monkeypatch.setenv(name, str(path))

    with pytest.raises(SettingsError, match="base64url-like"):
        Settings.from_env_and_secrets()


@pytest.mark.parametrize(
    ("provider", "conflicting_environment"),
    (
        ("builtin", {"DGX_AGENT_CA_CREDENTIAL_FILE": "/run/secrets/agent-ca-credential"}),
        ("step-ca", {"DGX_AGENT_BUILTIN_CA_BOOTSTRAP": "1"}),
        ("step-ca", {"DGX_AGENT_INTERMEDIATE_KEY_FILE": "/run/secrets/agent-intermediate-key"}),
    ),
)
def test_agent_ca_provider_rejects_other_provider_settings(
    monkeypatch,
    provider: str,
    conflicting_environment: dict[str, str],
) -> None:
    monkeypatch.setenv("DGX_DATABASE_URL", "postgresql://db/control")
    monkeypatch.setenv("DGX_AGENT_CA_PROVIDER", provider)
    if provider == "builtin":
        monkeypatch.setenv("DGX_AGENT_BUILTIN_CA_BOOTSTRAP", "1")
    for name, value in conflicting_environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(SettingsError, match="CA provider settings cannot be combined"):
        Settings.from_env_and_secrets()


def test_agent_proxy_auth_defaults_empty_and_production_rejects_builtin_ca(monkeypatch) -> None:
    monkeypatch.setenv("DGX_DATABASE_URL", "postgresql://db/control")
    settings = Settings.from_env_and_secrets()
    assert settings.agent_proxy_auth == b""

    monkeypatch.setenv("DGX_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("DGX_AGENT_CA_PROVIDER", "builtin")
    with pytest.raises(SettingsError, match="explicit bootstrap"):
        Settings.from_env_and_secrets()


def test_production_builtin_bootstrap_requires_and_loads_the_mounted_intermediate_key(tmp_path: Path, monkeypatch) -> None:
    values = {
        "DGX_DATABASE_URL_FILE": "postgresql://db/control",
        "DGX_TOKEN_SIGNING_KEY_FILE": "k" * 32,
        "DGX_METRICS_TOKEN_FILE": "m" * 16,
        "DGX_GIT_SIGNING_KEY_FILE": "git-key",
        "DGX_AGENT_CLIENT_CA_FILE": "client-ca",
        "DGX_AGENT_INTERMEDIATE_CERTIFICATE_FILE": "intermediate-certificate",
        "DGX_AGENT_INTERMEDIATE_KEY_FILE": "built-in-key",
        "DGX_AGENT_PROXY_AUTH_FILE": "p" * 32,
    }
    monkeypatch.setenv("DGX_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("DGX_AGENT_CA_PROVIDER", "builtin")
    monkeypatch.setenv("DGX_AGENT_BUILTIN_CA_BOOTSTRAP", "1")
    for name, value in values.items():
        path = tmp_path / name
        path.write_text(value)
        monkeypatch.setenv(name, str(path))

    settings = Settings.from_env_and_secrets()

    assert settings.agent_ca_provider == "builtin"
    assert settings.agent_intermediate_key_path == tmp_path / "DGX_AGENT_INTERMEDIATE_KEY_FILE"


def test_production_worker_settings_can_explicitly_disable_agent_runtime(tmp_path: Path, monkeypatch) -> None:
    values = {
        "DGX_DATABASE_URL_FILE": "postgresql://db/control",
        "DGX_TOKEN_SIGNING_KEY_FILE": "k" * 32,
        "DGX_METRICS_TOKEN_FILE": "m" * 16,
        "DGX_GIT_SIGNING_KEY_FILE": "git-key",
    }
    monkeypatch.setenv("DGX_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("DGX_AGENT_CA_PROVIDER", "step-ca")
    monkeypatch.setenv("DGX_AGENT_RUNTIME", "disabled")
    for name, value in values.items():
        path = tmp_path / name
        path.write_text(value)
        monkeypatch.setenv(name, str(path))

    settings = Settings.from_env_and_secrets()

    assert settings.agent_ca_provider == "step-ca"
    assert settings.agent_proxy_auth == b""


def test_production_requires_an_explicit_agent_ca_provider(monkeypatch) -> None:
    monkeypatch.setenv("DGX_DEPLOYMENT_MODE", "production")

    with pytest.raises(SettingsError, match="DGX_AGENT_CA_PROVIDER"):
        Settings.from_env_and_secrets()


def test_production_rejects_an_invalid_agent_ca_provider(monkeypatch) -> None:
    monkeypatch.setenv("DGX_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("DGX_AGENT_CA_PROVIDER", "unknown")

    with pytest.raises(SettingsError, match="is invalid"):
        Settings.from_env_and_secrets()


@pytest.mark.parametrize("key_kind", ("symlink", "directory"))
def test_builtin_bootstrap_key_must_be_a_regular_non_symlink_file(tmp_path: Path, monkeypatch, key_kind: str) -> None:
    values = {
        "DGX_DATABASE_URL_FILE": "postgresql://db/control",
        "DGX_TOKEN_SIGNING_KEY_FILE": "k" * 32,
        "DGX_METRICS_TOKEN_FILE": "m" * 16,
        "DGX_GIT_SIGNING_KEY_FILE": "git-key",
        "DGX_AGENT_CLIENT_CA_FILE": "client-ca",
        "DGX_AGENT_INTERMEDIATE_CERTIFICATE_FILE": "intermediate-certificate",
        "DGX_AGENT_PROXY_AUTH_FILE": "p" * 32,
    }
    monkeypatch.setenv("DGX_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("DGX_AGENT_CA_PROVIDER", "builtin")
    monkeypatch.setenv("DGX_AGENT_BUILTIN_CA_BOOTSTRAP", "1")
    for name, value in values.items():
        path = tmp_path / name
        path.write_text(value)
        monkeypatch.setenv(name, str(path))
    key = tmp_path / "agent-intermediate-key"
    if key_kind == "symlink":
        target = tmp_path / "actual-key"
        target.write_text("key")
        key.symlink_to(target)
    else:
        key.mkdir()
    monkeypatch.setenv("DGX_AGENT_INTERMEDIATE_KEY_FILE", str(key))

    with pytest.raises(SettingsError, match="regular non-symlink"):
        Settings.from_env_and_secrets()
