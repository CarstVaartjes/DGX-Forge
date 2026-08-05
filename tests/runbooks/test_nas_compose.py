from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_README = ROOT / "deploy/compose/README.md"
ENVIRONMENT = ROOT / "deploy/compose/.env.example"


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
    ):
        assert required in text


def test_environment_requires_three_release_images_without_duplicate_networks() -> None:
    text = ENVIRONMENT.read_text()
    assert "CONTROL_API_IMAGE=ghcr.io/carstvaartjes/dgx-forge-api:" in text
    assert "CONTROL_WORKER_IMAGE=ghcr.io/carstvaartjes/dgx-forge-worker:" in text
    assert "HERMES_AGENT_IMAGE=ghcr.io/carstvaartjes/dgx-forge-hermes:" in text
    assert text.count("DGX_MANAGEMENT_CIDRS=") == 1
    assert text.count("DGX_DIRECT_FABRIC_CIDRS=") == 1
