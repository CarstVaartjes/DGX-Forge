import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _environment() -> dict[str, str]:
    return os.environ | {
        "POSTGRES_IMAGE": "postgres:17@sha256:" + "a" * 64,
        "CADDY_IMAGE": "caddy:2@sha256:" + "b" * 64,
        "CONTROL_IMAGE": "example/control:1@sha256:" + "c" * 64,
        "LITELLM_IMAGE": "example/litellm:1@sha256:" + "d" * 64,
        "PROMETHEUS_IMAGE": "prom/prometheus:1@sha256:" + "e" * 64,
        "GRAFANA_IMAGE": "grafana/grafana:1@sha256:" + "f" * 64,
        "STEP_CA_IMAGE": "smallstep/step-ca:0.30.2@sha256:" + "1" * 64,
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
        "AGENT_CLIENT_CA_FILE": "/dev/null",
        "AGENT_INTERMEDIATE_CERTIFICATE_FILE": "/dev/null",
        "AGENT_PROXY_AUTH_FILE": "/dev/null",
        "AGENT_CA_CREDENTIAL_FILE": "/dev/null",
        "STEP_CA_INTERMEDIATE_KEY_FILE": "/dev/null",
        "STEP_CA_PASSWORD_FILE": "/dev/null",
        "STEP_CA_ROOT_CERTIFICATE_FILE": "/dev/null",
        "AGENT_INTERMEDIATE_KEY_FILE": "/dev/null",
        "DGX_CONTROL_HOSTNAME": "control.test.example",
        "DGX_AGENT_ENROLL_HOSTNAME": "enroll.test.example",
        "DGX_AGENT_HOSTNAME": "agents.test.example",
        "DGX_AGENT_PROXY_AUTH": "test-proxy-secret",
    }


def _rendered(*files: str) -> dict:
    command = ["docker", "compose"]
    for file in files or ("compose.yaml",):
        command.extend(("-f", str(ROOT / "deploy/compose" / file)))
    command.extend(("config", "--format", "json"))
    result = subprocess.run(command, check=True, capture_output=True, text=True, env=_environment())
    return json.loads(result.stdout)


def _adapted_caddy() -> dict:
    result = subprocess.run(
        [
            "docker", "run", "--rm", "-i",
            "-e", "DGX_CONTROL_HOSTNAME=control.test.example",
            "-e", "DGX_AGENT_ENROLL_HOSTNAME=enroll.test.example",
            "-e", "DGX_AGENT_HOSTNAME=agents.test.example",
            "-e", "DGX_AGENT_PROXY_AUTH=test-proxy-secret",
            "caddy:2.10.2@sha256:c3d7ee5d2b11f9dc54f947f68a734c84e9c9666c92c88a7f30b9cba5da182adb",
            "caddy", "adapt", "--config", "-", "--adapter", "caddyfile",
        ],
        check=True,
        capture_output=True,
        text=True,
        input=(ROOT / "deploy/compose/Caddyfile").read_text(),
    )
    return json.loads(result.stdout)


def test_caddy_adapts_three_sni_boundaries_for_admin_enrollment_and_mtls_agents() -> None:
    adapted = _adapted_caddy()
    server = adapted["apps"]["http"]["servers"]["srv0"]

    def site(host: str) -> dict:
        return next(route for route in server["routes"] if route.get("match") == [{"host": [host]}])

    control_site = next(
        route for route in server["routes"] if route.get("match") == [{"host": ["control.test.example"]}]
    )
    control_routes = control_site["handle"][0]["routes"]
    denied = next(
        index for index, route in enumerate(control_routes)
        if route.get("match") == [{"path": ["/agent/v1/*"]}]
    )
    fallback = next(
        index for index, route in enumerate(control_routes)
        if "control-api:8000" in json.dumps(route, sort_keys=True)
    )
    assert denied < fallback

    enrollment_routes = site("enroll.test.example")["handle"][0]["routes"]
    enrollment_proxy = next(route for route in enrollment_routes if "control-api:8000" in json.dumps(route, sort_keys=True))
    assert enrollment_proxy["match"] == [{"path": ["/agent/v1/enroll"]}]
    assert any(route.get("match") == [{"not": [{"path": ["/agent/v1/enroll"]}]}] for route in enrollment_routes)

    agent_site = site("agents.test.example")
    client_auth = next(
        policy["client_authentication"]
        for policy in server["tls_connection_policies"]
        if policy.get("match") == {"sni": ["agents.test.example"]}
    )
    assert client_auth["mode"] == "require_and_verify"
    agent_routes = agent_site["handle"][0]["routes"]
    agent_proxy = next(route for route in agent_routes if "control-api:8000" in json.dumps(route, sort_keys=True))
    request_headers = agent_proxy["handle"][0]["routes"][0]["handle"][0]["headers"]["request"]
    assert request_headers["delete"] == ["X-DGX-Agent-*"]
    replacements = {key.lower(): value for key, value in request_headers["set"].items()}
    assert replacements == {
        "x-dgx-agent-node": ["{dgx_agent_node}"],
        "x-dgx-agent-serial": ["{http.request.tls.client.serial}"],
        "x-dgx-agent-fingerprint": ["{http.request.tls.client.fingerprint}"],
        "x-dgx-agent-verified": ["1"],
        "x-dgx-agent-proxy-auth": ["test-proxy-secret"],
    }
    assert any(route.get("match") == [{"not": [{"path": ["/agent/v1/enroll"]}], "path": ["/agent/v1/*"]}] for route in agent_routes)


def test_rendered_production_boundary_has_only_caddy_public_and_step_ca_private() -> None:
    rendered = _rendered()
    services = rendered["services"]
    assert {name for name, service in services.items() if service.get("ports")} == {"caddy"}
    assert set(services["caddy"]["networks"]) == {"agent-proxy", "ingress"}
    assert set(services["control-api"]["networks"]) == {"agent-proxy", "application", "ca", "data"}
    assert rendered["networks"]["agent-proxy"]["internal"] is True
    assert "step-ca" in services
    assert not services["step-ca"].get("ports")
    assert {secret["source"] for secret in services["caddy"]["secrets"]} >= {"agent-client-ca", "agent-proxy-auth"}
    assert {secret["source"] for secret in services["control-api"]["secrets"]} >= {
        "agent-client-ca", "agent-intermediate-certificate", "agent-ca-credential", "agent-proxy-auth",
    }
    assert "agent-intermediate-key" not in {secret["source"] for secret in services["control-api"]["secrets"]}
    assert "root-private" not in json.dumps(services["step-ca"], sort_keys=True).lower()


def test_builtin_ca_override_is_explicit_and_only_it_mounts_the_builtin_signing_key() -> None:
    production = _rendered()
    builtin = _rendered("compose.yaml", "compose.builtin-ca.yaml")
    production_secrets = {secret["source"] for secret in production["services"]["control-api"]["secrets"]}
    builtin_secrets = {secret["source"] for secret in builtin["services"]["control-api"]["secrets"]}
    assert "agent-intermediate-key" not in production_secrets
    assert "agent-intermediate-key" in builtin_secrets
    assert builtin["services"]["control-api"]["environment"]["DGX_AGENT_CA_PROVIDER"] == "builtin"
    assert builtin["services"]["control-api"]["environment"]["DGX_AGENT_BUILTIN_CA_BOOTSTRAP"] == "1"
