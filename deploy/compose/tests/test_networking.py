import json
import os
import subprocess
from pathlib import Path


def _rendered() -> dict:
    root = Path(__file__).resolve().parents[3]
    env = os.environ | {
        "POSTGRES_IMAGE": "postgres:17@sha256:" + "a" * 64,
        "CADDY_IMAGE": "caddy:2@sha256:" + "b" * 64,
        "CONTROL_IMAGE": "example/control:1@sha256:" + "c" * 64,
        "REPOSITORY_PATH": "/srv/dgx-forge/repository",
        "DATABASE_URL_FILE": "/dev/null",
        "POSTGRES_PASSWORD_FILE": "/dev/null",
        "TOKEN_SIGNING_KEY_FILE": "/dev/null",
    }
    result = subprocess.run(
        ["docker", "compose", "-f", str(root / "deploy/compose/compose.yaml"), "config", "--format", "json"],
        check=True, capture_output=True, text=True, env=env,
    )
    return json.loads(result.stdout)


def test_only_caddy_publishes_ports_and_images_are_digest_pinned() -> None:
    rendered = _rendered()
    published = {name for name, service in rendered["services"].items() if service.get("ports")}
    assert published == {"caddy"}
    assert all("@sha256:" in service["image"] for service in rendered["services"].values())


def test_database_has_only_data_network_and_ingress_is_segmented() -> None:
    services = _rendered()["services"]
    assert set(services["postgres"]["networks"]) == {"data"}
    assert set(services["caddy"]["networks"]) == {"ingress"}
    assert set(services["control-worker"]["networks"]) == {"application", "data"}
    assert set(services["control-api"]["networks"]) == {"application", "data", "ingress"}


def test_caddy_disables_admin_and_sets_edge_guards() -> None:
    root = Path(__file__).resolve().parents[3]
    text = (root / "deploy/compose/Caddyfile").read_text()
    assert "admin off" in text
    assert "max_size 1MB" in text
    assert "Strict-Transport-Security" in text
    assert "X-Frame-Options" in text
