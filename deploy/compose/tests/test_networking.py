import json
import os
import subprocess
from pathlib import Path


def _rendered() -> dict:
    root = Path(__file__).resolve().parents[3]
    env = os.environ | {
        "POSTGRES_IMAGE": "postgres:17@sha256:" + "a" * 64,
        "CADDY_IMAGE": "caddy:2@sha256:" + "b" * 64,
        "REGISTRY_IMAGE": "registry:3@sha256:" + "9" * 64,
        "CONTROL_IMAGE": "example/control:1@sha256:" + "c" * 64,
        "LITELLM_IMAGE": "example/litellm:1@sha256:" + "d" * 64,
        "PROMETHEUS_IMAGE": "prom/prometheus:1@sha256:" + "e" * 64,
        "GRAFANA_IMAGE": "grafana/grafana:1@sha256:" + "f" * 64,
        "REPOSITORY_PATH": "/srv/dgx-forge/repository",
        "DATABASE_URL_FILE": "/dev/null",
        "POSTGRES_PASSWORD_FILE": "/dev/null",
        "TOKEN_SIGNING_KEY_FILE": "/dev/null",
        "METRICS_TOKEN_FILE": "/dev/null",
        "GIT_SIGNING_KEY_FILE": "/dev/null",
        "GRAFANA_ADMIN_PASSWORD_FILE": "/dev/null",
        "LITELLM_MASTER_KEY_FILE": "/dev/null",
        "LITELLM_UPSTREAM_KEY_FILE": "/dev/null",
        "LITELLM_DATABASE_URL_FILE": "/dev/null",
        "STEP_CA_IMAGE": "smallstep/step-ca:0.30.2@sha256:" + "1" * 64,
        "TAILSCALE_IMAGE": "tailscale/tailscale:v1.98.8@sha256:d54b2e6a9c09f0e5ec52e82b9ad4af3d446b54a7c08075e92f11c39dd410105f",
        "AGENT_CLIENT_CA_FILE": "/dev/null",
        "AGENT_INTERMEDIATE_CERTIFICATE_FILE": "/dev/null",
        "AGENT_PROXY_AUTH_FILE": "/dev/null",
        "AGENT_CA_CREDENTIAL_FILE": "/dev/null",
        "AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE": "/dev/null",
        "AGENT_CA_PROVISIONER_KID": "test-provisioner-kid",
        "STEP_CA_CONFIG_FILE": "/dev/null",
        "STEP_CA_ROOT_CERTIFICATE_FILE": "/dev/null",
        "STEP_CA_INTERMEDIATE_KEY_FILE": "/dev/null",
        "STEP_CA_PASSWORD_FILE": "/dev/null",
        "DGX_CONTROL_HOSTNAME": "control.test.example",
        "DGX_AGENT_ENROLL_HOSTNAME": "enroll.test.example",
        "DGX_AGENT_HOSTNAME": "agents.test.example",
        "DGX_REGISTRY_HOSTNAME": "registry.test.example",
        "DGX_MANAGEMENT_CIDRS": "10.0.0.0/24",
        "DGX_DIRECT_FABRIC_CIDRS": "192.168.100.0/24,192.168.101.0/24",
        "NAS_LAN_IP": "10.0.0.2",
        "DGX_BACKEND_PORT": "8443",
        "TAILSCALE_OAUTH_CLIENT_ID_FILE": "/dev/null",
        "TAILSCALE_OAUTH_CLIENT_SECRET_FILE": "/dev/null",
        "AI_DEVBOX_UID": "1100",
        "AI_DEVBOX_GID": "1100",
        "AI_DEVBOX_DATA_ROOT": "/srv/dgx-forge/ai-devbox",
        "AI_DEVBOX_AUTHORIZED_KEYS_FILE": "/srv/dgx-forge/secrets/ai-devbox-authorized-keys",
    }
    result = subprocess.run(
        ["docker", "compose", "-f", str(root / "deploy/compose/compose.yaml"), "-f", str(root / "deploy/compose/compose.step-ca.yaml"), "config", "--format", "json"],
        check=True, capture_output=True, text=True, env=env,
    )
    return json.loads(result.stdout)


def test_only_caddy_publishes_ports_and_images_are_digest_pinned() -> None:
    rendered = _rendered()
    published = {name for name, service in rendered["services"].items() if service.get("ports")}
    assert published == {"caddy"}
    assert all(
        "@sha256:" in service["image"] or service.get("build")
        for service in rendered["services"].values()
    )


def test_caddy_publishes_only_reserved_nas_backend_listener() -> None:
    caddy = _rendered()["services"]["caddy"]

    assert caddy["ports"] == [
        {
            "mode": "ingress",
            "target": 8443,
            "published": "8443",
            "protocol": "tcp",
            "host_ip": "10.0.0.2",
        }
    ]
    assert caddy["environment"]["DGX_BACKEND_PORT"] == "8443"


def test_database_has_only_data_network_and_ingress_is_segmented() -> None:
    services = _rendered()["services"]
    assert set(services["postgres"]["networks"]) == {"data"}
    assert set(services["caddy"]["networks"]) == {"agent-proxy", "ingress", "registry-edge", "tailnet-web-edge"}
    assert set(services["registry"]["networks"]) == {"registry-edge", "registry-publisher"}
    assert set(services["control-worker"]["networks"]) == {"application", "cluster-egress", "data"}
    assert set(services["control-api"]["networks"]) == {"agent-proxy", "application", "ca", "data"}
    assert set(services["litellm"]["networks"]) == {"cluster-egress", "data", "ingress"}
    assert set(services["prometheus"]["networks"]) == {"application"}


def test_tailnet_backends_have_readiness_checks() -> None:
    services = _rendered()["services"]

    assert services["caddy"]["healthcheck"]["test"] == [
        "CMD-SHELL",
        "wget -q -O /dev/null http://127.0.0.1:8080/healthz",
    ]
    assert services["ai-devbox"]["healthcheck"]["test"] == [
        "CMD-SHELL",
        "ssh-keyscan -T 3 -p 22 127.0.0.1 >/dev/null 2>&1",
    ]


def test_litellm_routes_use_a_dedicated_atomic_config_volume() -> None:
    services = _rendered()["services"]
    worker_volumes = {volume["target"]: volume for volume in services["control-worker"]["volumes"]}
    litellm_volumes = {volume["target"]: volume for volume in services["litellm"]["volumes"]}

    assert worker_volumes["/litellm-routes"]["source"] == "litellm-routes"
    assert litellm_volumes["/routes"] == {
        "type": "volume",
        "source": "litellm-routes",
        "target": "/routes",
        "read_only": True,
        "volume": {},
    }
    assert services["control-worker"]["environment"]["DGX_LITELLM_CONFIG_PATH"] == "/litellm-routes/config.yaml"
    assert "litellm-upstream-key" in {
        secret["source"] for secret in services["control-worker"]["secrets"]
    }
    initializer = services["litellm-routes-init"]
    assert initializer["network_mode"] == "none"
    assert initializer["cap_drop"] == ["ALL"]
    assert set(initializer["cap_add"]) == {"CHOWN", "FOWNER"}
    assert initializer["security_opt"] == ["no-new-privileges:true"]


def test_caddy_disables_admin_and_sets_edge_guards() -> None:
    root = Path(__file__).resolve().parents[3]
    text = (root / "deploy/compose/Caddyfile").read_text()
    assert "admin off" in text
    assert "max_size 1MB" in text
    assert "Strict-Transport-Security" in text
    assert "X-Frame-Options" in text
