from __future__ import annotations

import json
import os
import socket
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
    assert volumes["/usr/local/bin/configure-tailscale"]["read_only"] is True
    assert configurator["restart"] == "unless-stopped"
    assert configurator["depends_on"] == {
        "ai-devbox": {
            "condition": "service_healthy",
            "required": True,
            "restart": True,
        },
        "caddy": {
            "condition": "service_healthy",
            "required": True,
            "restart": True,
        },
        "tailscale-gateway": {
            "condition": "service_healthy",
            "required": True,
            "restart": True,
        },
    }


def test_service_map_and_configurator_are_exact_and_fail_closed() -> None:
    script = COMPOSE / "tailscale/configure.sh"
    subprocess.run(["/bin/sh", "-n", script], check=True)
    text = script.read_text()
    # The configuration-file form cannot distinguish HTTPS termination from a
    # plaintext listener when its upstream is HTTP (tailscale/tailscale#18381).
    assert "serve set-config" not in text
    assert "--service=svc:dgx-forge --https=443 http://caddy:8080" in text
    assert "--service=svc:ai-devbox --tcp=22 tcp://ai-devbox:22" in text
    assert "serve advertise svc:dgx-forge" in text
    assert "serve advertise svc:ai-devbox" in text
    assert "serve get-config --all" in text
    assert "serve reset" in text
    assert '"svc:ai-devbox":{"endpoints":{"tcp:22":"tcp://ai-devbox:22"}}' in text
    assert '"svc:dgx-forge":{"endpoints":{"tcp:443":"http://caddy:8080"}}' in text
    assert '"HTTPS":true' in text
    assert '"HTTP":true' in text
    assert "120" in text
    assert "service-host" in text
    assert "svc:*" not in text


def test_configurator_repairs_plaintext_443_and_verifies_https(tmp_path: Path) -> None:
    socket_path = tmp_path / "tailscaled.sock"
    daemon_socket = socket.socket(socket.AF_UNIX)
    daemon_socket.bind(str(socket_path))
    log = tmp_path / "calls.log"
    repaired = tmp_path / "repaired"
    fake = tmp_path / "tailscale"
    fake.write_text(
        "#!/bin/sh\n"
        f"log={log}\n"
        f"repaired={repaired}\n"
        "case \"$*\" in\n"
        "  *\"serve get-config --all\"*)\n"
        "    if [ -f \"$repaired\" ]; then\n"
        "      printf '%s\\n' '{\"version\":\"0.0.1\",\"services\":{\"svc:ai-devbox\":{\"endpoints\":{\"tcp:22\":\"tcp://ai-devbox:22\"}},\"svc:dgx-forge\":{\"endpoints\":{\"tcp:443\":\"http://caddy:8080\"}}}}'\n"
        "    else\n"
        "      printf '%s\\n' '{\"version\":\"0.0.1\",\"services\":{\"svc:extra\":{\"endpoints\":{\"tcp:99\":\"tcp://unexpected:99\"}}}}'\n"
        "    fi ;;\n"
        "  *\"serve status --json\"*)\n"
        "    if [ -f \"$repaired\" ]; then\n"
        "      printf '%s\\n' '{\"Services\":{\"svc:ai-devbox\":{\"TCP\":{\"22\":{\"TCPForward\":\"ai-devbox:22\"}}},\"svc:dgx-forge\":{\"TCP\":{\"443\":{\"HTTPS\":true}}}}}'\n"
        "    else\n"
        "      printf '%s\\n' '{\"Services\":{\"svc:ai-devbox\":{\"TCP\":{\"22\":{\"TCPForward\":\"ai-devbox:22\"}}},\"svc:dgx-forge\":{\"TCP\":{\"443\":{\"HTTP\":true}}}}}'\n"
        "    fi ;;\n"
        "  *\"--service=svc:dgx-forge --https=443 http://caddy:8080\"*)\n"
        "    printf '%s\\n' \"$*\" >>\"$log\"; touch \"$repaired\" ;;\n"
        "  *\"serve --service=svc:ai-devbox --tcp=22 tcp://ai-devbox:22\"*)\n"
        "    printf '%s\\n' \"$*\" >>\"$log\" ;;\n"
        "  *\"status --json\"*) printf '%s\\n' '{\"Capabilities\":[\"service-host\"]}' ;;\n"
        "  *) printf '%s\\n' \"$*\" >>\"$log\" ;;\n"
        "esac\n"
    )
    fake.chmod(0o755)
    try:
        result = subprocess.run(
            ["/bin/sh", COMPOSE / "tailscale/configure.sh"],
            env=os.environ
            | {
                "PATH": f"{tmp_path}:{os.environ['PATH']}",
                "TS_CONFIGURE_ONCE": "1",
                "TS_SOCKET_PATH": str(socket_path),
            },
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        daemon_socket.close()

    assert result.returncode == 0, result.stderr
    calls = log.read_text()
    assert "--service=svc:dgx-forge --https=443 http://caddy:8080" in calls
    assert "--service=svc:ai-devbox --tcp=22 tcp://ai-devbox:22" in calls
    assert "serve reset" in calls
    assert "set-config" not in calls


def test_grants_example_is_exact_service_least_privilege() -> None:
    policy = json.loads((COMPOSE / "tailscale/grants.example.hujson").read_text())

    assert policy["tagOwners"] == {"tag:dgx-gateway": ["autogroup:admin"]}
    assert policy["groups"] == {
        "group:ai-devbox-users": ["replace-with-your-login@github"]
    }
    assert policy["acls"] == []
    assert policy["grants"] == [
        {
            "src": ["autogroup:admin"],
            "dst": ["svc:dgx-forge"],
            "ip": ["tcp:443"],
        },
        {
            "src": ["group:ai-devbox-users"],
            "dst": ["svc:ai-devbox"],
            "ip": ["tcp:22"],
        },
    ]
    assert policy["autoApprovers"] == {
        "services": {
            "svc:dgx-forge": ["tag:dgx-gateway"],
            "svc:ai-devbox": ["tag:dgx-gateway"],
        }
    }
    assert policy["tests"] == [
        {"src": "autogroup:admin", "accept": ["svc:dgx-forge:443"]},
        {"src": "autogroup:member", "deny": ["svc:dgx-forge:443"]},
        {
            "src": "replace-with-your-login@github",
            "accept": ["svc:ai-devbox:22"],
        },
        {"src": "autogroup:member", "deny": ["svc:ai-devbox:22"]},
    ]
    assert "svc:*" not in json.dumps(policy)
    assert "tskey-" not in json.dumps(policy).lower()
