from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ROOT / "deploy/compose"
TAILSCALE_IMAGE = (
    "tailscale/tailscale:v1.98.8@sha256:"
    "d54b2e6a9c09f0e5ec52e82b9ad4af3d446b54a7c08075e92f11c39dd410105f"
)


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    for line in (COMPOSE / "tests/test.env").read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            environment[key] = value
    return environment


def _rendered() -> dict[str, object]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE / "compose.yaml"),
            "-f",
            str(COMPOSE / "compose.step-ca.yaml"),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=_environment(),
    )
    return json.loads(result.stdout)


def _volume_targets(service: dict[str, object]) -> dict[str, dict[str, object]]:
    return {volume["target"]: volume for volume in service.get("volumes", [])}


def test_gateway_is_persistent_userspace_and_unpublished() -> None:
    rendered = _rendered()
    services = rendered["services"]
    gateway = services["tailscale-gateway"]

    assert gateway["image"] == TAILSCALE_IMAGE
    assert gateway["read_only"] is True
    assert not gateway.get("ports")
    assert not gateway.get("devices")
    assert not gateway.get("cap_add")
    assert set(gateway["networks"]) == {"tailnet-web-edge", "tailnet-ssh-edge"}
    assert gateway["environment"] == {
        "TS_AUTH_ONCE": "true",
        "TS_CLIENT_ID": "file:/run/secrets/tailscale-oauth-client-id",
        "TS_CLIENT_SECRET": "file:/run/secrets/tailscale-oauth-client-secret",
        "TS_EXTRA_ARGS": "--advertise-tags=tag:dgx-gateway",
        "TS_HOSTNAME": "dgx-forge-gateway",
        "TS_SOCKET": "/var/run/tailscale/tailscaled.sock",
        "TS_STATE_DIR": "/var/lib/tailscale",
        "TS_USERSPACE": "true",
    }
    volumes = _volume_targets(gateway)
    assert volumes["/var/lib/tailscale"]["type"] == "volume"
    assert volumes["/var/run/tailscale"]["type"] == "volume"
    secret_targets = {secret["target"] for secret in gateway["secrets"]}
    assert secret_targets == {
        "/run/secrets/tailscale-oauth-client-id",
        "/run/secrets/tailscale-oauth-client-secret",
    }


def test_configurator_shares_gateway_namespace_and_socket() -> None:
    configurator = _rendered()["services"]["tailscale-configurator"]

    assert configurator["image"] == TAILSCALE_IMAGE
    assert configurator["network_mode"] == "service:tailscale-gateway"
    assert configurator["read_only"] is True
    assert not configurator.get("ports")
    assert not configurator.get("networks")
    assert not configurator.get("devices")
    assert not configurator.get("cap_add")
    volumes = _volume_targets(configurator)
    assert volumes["/var/run/tailscale"]["type"] == "volume"
    assert volumes["/config"]["read_only"] is True
    assert volumes["/usr/local/bin/configure-tailscale"]["read_only"] is True


def test_service_map_and_configurator_are_exact_and_fail_closed() -> None:
    serve = json.loads((COMPOSE / "tailscale/serve.json").read_text())
    assert serve == {
        "version": "0.0.1",
        "services": {
            "svc:dgx-forge": {
                "endpoints": {"tcp:443": "http://caddy:8080"},
            }
        },
    }
    script = COMPOSE / "tailscale/configure.sh"
    subprocess.run(["/bin/sh", "-n", script], check=True)
    text = script.read_text()
    assert "serve set-config --all /config/serve.json" in text
    assert "serve advertise svc:dgx-forge" in text
    assert "120" in text
    assert "service-host" in text
    assert "svc:*" not in text


def test_grants_example_is_exact_service_least_privilege() -> None:
    policy = json.loads((COMPOSE / "tailscale/grants.example.hujson").read_text())

    assert policy["tagOwners"] == {"tag:dgx-gateway": ["autogroup:admin"]}
    assert policy["acls"] == []
    assert policy["grants"] == [
        {
            "src": ["autogroup:admin"],
            "dst": ["svc:dgx-forge"],
            "ip": ["tcp:443"],
        }
    ]
    assert policy["autoApprovers"] == {
        "services": {"svc:dgx-forge": ["tag:dgx-gateway"]}
    }
    assert policy["tests"] == [
        {"src": "autogroup:admin", "accept": ["svc:dgx-forge:443"]},
        {"src": "autogroup:member", "deny": ["svc:dgx-forge:443"]},
    ]
    assert "svc:*" not in json.dumps(policy)
    assert "github" not in json.dumps(policy).lower()
