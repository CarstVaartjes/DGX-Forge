from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_README = ROOT / "deploy/compose/README.md"
ENVIRONMENT = ROOT / "deploy/compose/.env.example"
SUPPLY_CHAIN = ROOT / "docs/runbooks/supply-chain.md"


def test_nas_compose_readme_is_the_complete_operator_entry_point() -> None:
    text = COMPOSE_README.read_text()
    for required in (
        "ghcr.io/carstvaartjes/dgx-forge-api",
        "ghcr.io/carstvaartjes/dgx-forge-worker",
        "ghcr.io/carstvaartjes/dgx-forge-hermes",
        "NAS_LAN_IP=10.0.0.2",
        "docker compose pull",
        "docker compose config --quiet",
        "compose.step-ca.yaml",
        "dgx-forge-images.env",
        "dgx-forge-images.env.sha256",
        "latest is evaluation-only",
        "Set package visibility to Public",
        "not the Docker bridge",
        "not the public WAN address",
        "DGX_CONTAINER_RELEASES_ENABLED",
        "No images are currently being published",
        "Dependabot cannot publish",
        "sudo install -d -m 0750 -o \"$USER\" -g \"$USER\" /srv/dgx-forge",
        "DATABASE_URL_FILE",
        "POSTGRES_PASSWORD_FILE",
        "TOKEN_SIGNING_KEY_FILE",
        "METRICS_TOKEN_FILE",
        "GIT_SIGNING_KEY_FILE",
        "WORKER_API_TOKEN_FILE",
        "GRAFANA_ADMIN_PASSWORD_FILE",
        "LITELLM_MASTER_KEY_FILE",
        "LITELLM_UPSTREAM_KEY_FILE",
        "LITELLM_DATABASE_URL_FILE",
        "At least 32 bytes.",
        "At least 16 non-whitespace characters.",
        "10001:10001",
        "10002:10001",
        "65534:65534",
        "1100:1100",
        "control.dgx-forge.lan is not a LAN-accessible human endpoint",
    ):
        assert required in text
    assert "\nsudo git clone " not in text
    migration = "dgx_control.offline --state-path /state migrate"
    assert text.index("up -d postgres") < text.index(migration)
    assert text.index(migration) < text.index(
        "create-admin --subject ADMIN_ID"
    ) < text.index("up -d\ndocker compose")


def test_environment_requires_three_release_images_without_duplicate_networks() -> None:
    text = ENVIRONMENT.read_text()
    assert "CONTROL_API_IMAGE=ghcr.io/carstvaartjes/dgx-forge-api:" in text
    assert "CONTROL_WORKER_IMAGE=ghcr.io/carstvaartjes/dgx-forge-worker:" in text
    assert "HERMES_AGENT_IMAGE=ghcr.io/carstvaartjes/dgx-forge-hermes:" in text
    assert text.count("DGX_MANAGEMENT_CIDRS=") == 1
    assert text.count("DGX_DIRECT_FABRIC_CIDRS=") == 1


def test_supply_chain_describes_three_target_release_or_nonpublishing_diagnostics() -> None:
    text = SUPPLY_CHAIN.read_text()
    for required in (
        "CONTROL_API_IMAGE",
        "CONTROL_WORKER_IMAGE",
        "HERMES_AGENT_IMAGE",
        "stable SemVer version-tag push",
        "dgx-forge-hermes",
        "local diagnostic only",
    ):
        assert required in text
    assert "Publish both immutable control images" not in text
