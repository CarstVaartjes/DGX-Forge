import json
import os
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _environment() -> dict[str, str]:
    return os.environ | {
        "POSTGRES_IMAGE": "postgres:17@sha256:" + "a" * 64,
        "CADDY_IMAGE": "caddy:2@sha256:" + "b" * 64,
        "REGISTRY_IMAGE": "registry:3@sha256:" + "9" * 64,
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
        "AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE": "/dev/null",
        "AGENT_CA_PROVISIONER_KID": "test-provisioner-kid",
        "STEP_CA_CONFIG_FILE": "/dev/null",
        "STEP_CA_INTERMEDIATE_KEY_FILE": "/dev/null",
        "STEP_CA_PASSWORD_FILE": "/dev/null",
        "STEP_CA_ROOT_CERTIFICATE_FILE": "/dev/null",
        "AGENT_INTERMEDIATE_KEY_FILE": "/dev/null",
        "DGX_CONTROL_HOSTNAME": "control.test.example",
        "DGX_AGENT_ENROLL_HOSTNAME": "enroll.test.example",
        "DGX_AGENT_HOSTNAME": "agents.test.example",
        "DGX_REGISTRY_HOSTNAME": "registry.test.example",
        "DGX_AGENT_PROXY_AUTH": "test-proxy-secret",
        "DGX_MANAGEMENT_CIDRS": "10.0.0.0/24",
    }


def _rendered(*files: str, environment: dict[str, str] | None = None) -> dict:
    command = ["docker", "compose"]
    for file in files or ("compose.yaml", "compose.step-ca.yaml"):
        command.extend(("-f", str(ROOT / "deploy/compose" / file)))
    command.extend(("config", "--format", "json"))
    result = subprocess.run(command, check=True, capture_output=True, text=True, env=environment or _environment())
    return json.loads(result.stdout)


def _adapted_caddy(environment: dict[str, str]) -> dict:
    result = subprocess.run(
        [
            "docker", "run", "--rm", "-i",
            "-e", f"DGX_CONTROL_HOSTNAME={environment['DGX_CONTROL_HOSTNAME']}",
            "-e", f"DGX_AGENT_ENROLL_HOSTNAME={environment['DGX_AGENT_ENROLL_HOSTNAME']}",
            "-e", f"DGX_AGENT_HOSTNAME={environment['DGX_AGENT_HOSTNAME']}",
            "-e", f"DGX_REGISTRY_HOSTNAME={environment['DGX_REGISTRY_HOSTNAME']}",
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


def _entrypoint_result(
    environment: dict[str, str],
    secret_source: str | None = None,
    entrypoint_arguments: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    environment = environment | {
        "DGX_REGISTRY_HOSTNAME": environment.get(
            "DGX_REGISTRY_HOSTNAME", "registry.test.example"
        )
    }
    command = ["docker", "run", "--rm"]
    for name, value in environment.items():
        command.extend(("-e", f"{name}={value}"))
    command.extend((
        "-v", f"{ROOT / 'deploy/compose/caddy/entrypoint.sh'}:/usr/local/bin/dgx-caddy-entrypoint:ro",
    ))
    if secret_source is not None:
        command.extend(("-v", f"{secret_source}:/run/secrets/agent-proxy-auth:ro"))
    command.extend((
        "caddy:2.10.2@sha256:c3d7ee5d2b11f9dc54f947f68a734c84e9c9666c92c88a7f30b9cba5da182adb",
        "/bin/sh", "/usr/local/bin/dgx-caddy-entrypoint",
    ))
    command.extend(entrypoint_arguments)
    return subprocess.run(
        command, capture_output=True, text=True, timeout=10, check=False
    )


def _settings_result(rendered: dict, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("DGX_")
    }
    control_environment = rendered["services"]["control-api"]["environment"].copy()
    secret_values = {
        "DGX_DATABASE_URL_FILE": "postgresql://control:pw@postgres/control\n",
        "DGX_TOKEN_SIGNING_KEY_FILE": "t" * 32 + "\n",
        "DGX_METRICS_TOKEN_FILE": "m" * 16 + "\n",
        "DGX_GIT_SIGNING_KEY_FILE": "test-git-key\n",
        "DGX_AGENT_CLIENT_CA_FILE": "test-client-ca\n",
        "DGX_AGENT_INTERMEDIATE_CERTIFICATE_FILE": "test-intermediate-certificate\n",
        "DGX_AGENT_CA_CREDENTIAL_FILE": "test-provider-credential\n",
        "DGX_AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE": "test-provider-public-jwk\n",
        "DGX_AGENT_CA_ROOT_FILE": "test-root-certificate\n",
        "DGX_AGENT_INTERMEDIATE_KEY_FILE": "test-builtin-key\n",
        "DGX_AGENT_PROXY_AUTH_FILE": "A" * 30 + "_-\r\n",
    }
    for name, value in tuple(control_environment.items()):
        if name not in secret_values:
            continue
        secret = tmp_path / name.lower()
        secret.write_text(secret_values[name])
        control_environment[name] = str(secret)
    control_environment.setdefault("DGX_AGENT_CA_PROVISIONER_NAME", "dgx-forge-agent")
    control_environment.setdefault("DGX_AGENT_CA_PROVISIONER_KID", "test-provisioner-kid")
    environment.update({name: str(value) for name, value in control_environment.items()})
    return subprocess.run(
        [
            "uv", "run", "--project", str(ROOT / "control"), "python", "-c",
            (
                "from dgx_control.settings import Settings; "
                "settings = Settings.from_env_and_secrets(); "
                "print(settings.agent_ca_provider); "
                "print(settings.agent_proxy_auth.decode('ascii'))"
            ),
        ],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=False,
    )


def test_caddy_adapts_three_sni_boundaries_for_admin_enrollment_and_mtls_agents() -> None:
    environment = _environment()
    rendered_caddy = _rendered("compose.yaml")["services"]["caddy"]
    caddy_environment = rendered_caddy["environment"]
    assert {name: caddy_environment[name] for name in (
        "DGX_CONTROL_HOSTNAME", "DGX_AGENT_ENROLL_HOSTNAME", "DGX_AGENT_HOSTNAME",
    )} == {name: environment[name] for name in (
        "DGX_CONTROL_HOSTNAME", "DGX_AGENT_ENROLL_HOSTNAME", "DGX_AGENT_HOSTNAME",
    )}
    adapted = _adapted_caddy(caddy_environment | {"DGX_AGENT_PROXY_AUTH": "test-proxy-secret"})
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
        if "agents.test.example" in policy.get("match", {}).get("sni", [])
    )
    assert client_auth["mode"] == "require_and_verify"
    assert client_auth["ca"] == {"provider": "file", "pem_files": ["/run/secrets/agent-client-ca"]}
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
        "x-dgx-agent-source": ["{http.request.remote.host}"],
    }
    assert any(route.get("match") == [{"not": [{"path": ["/agent/v1/enroll"]}], "path": ["/agent/v1/*"]}] for route in agent_routes)
    mappings = []

    def collect_maps(value: object) -> None:
        if isinstance(value, dict):
            if value.get("handler") == "map":
                mappings.append(value)
            for child in value.values():
                collect_maps(child)
        elif isinstance(value, list):
            for child in value:
                collect_maps(child)

    collect_maps(adapted)
    assert mappings == [{
        "handler": "map", "source": "{http.request.tls.client.subject}",
        "destinations": ["{dgx_agent_node}"], "defaults": [""],
        "mappings": [{"input_regexp": "^CN=(spk_[0-9a-f]{32})$", "outputs": ["${1}"]}],
    }]


def test_caddy_activation_route_is_exposed_only_on_verified_mtls_agent_sni() -> None:
    caddy_environment = _rendered("compose.yaml")["services"]["caddy"][
        "environment"
    ]
    adapted = _adapted_caddy(
        caddy_environment | {"DGX_AGENT_PROXY_AUTH": "test-proxy-secret"}
    )
    server = adapted["apps"]["http"]["servers"]["srv0"]
    activation_path = "/agent/v1/renew/activate"

    def site(host: str) -> dict:
        return next(
            route
            for route in server["routes"]
            if route.get("match") == [{"host": [host]}]
        )

    agent_policy = next(
        policy
        for policy in server["tls_connection_policies"]
        if "agents.test.example" in policy.get("match", {}).get("sni", [])
    )
    assert agent_policy["client_authentication"]["mode"] == "require_and_verify"

    agent_routes = site("agents.test.example")["handle"][0]["routes"]
    agent_proxy = next(
        route
        for route in agent_routes
        if "control-api:8000" in json.dumps(route, sort_keys=True)
    )
    agent_path_pattern = agent_proxy["match"][0]["path"][0]
    assert fnmatchcase(activation_path, agent_path_pattern)

    enrollment_routes = site("enroll.test.example")["handle"][0]["routes"]
    enrollment_proxy = next(
        route
        for route in enrollment_routes
        if "control-api:8000" in json.dumps(route, sort_keys=True)
    )
    assert not fnmatchcase(activation_path, enrollment_proxy["match"][0]["path"][0])

    control_routes = site("control.test.example")["handle"][0]["routes"]
    control_denial = next(
        route
        for route in control_routes
        if fnmatchcase(
            activation_path,
            route.get("match", [{}])[0].get("path", [""])[0],
        )
    )
    assert '"handler": "static_response"' in json.dumps(
        control_denial, sort_keys=True
    )
    assert '"status_code": 404' in json.dumps(control_denial, sort_keys=True)


def test_caddy_compose_requires_distinct_sni_hostnames_before_startup(tmp_path: Path) -> None:
    missing = _environment()
    missing.pop("DGX_AGENT_HOSTNAME")
    command = ["docker", "compose", "-f", str(ROOT / "deploy/compose/compose.yaml"), "config", "--quiet"]
    absent = subprocess.run(
        command, capture_output=True, text=True, env=missing, check=False
    )
    assert absent.returncode != 0
    assert "DGX_AGENT_HOSTNAME" in absent.stderr

    for duplicate in (
        {"DGX_CONTROL_HOSTNAME": "same.test.example", "DGX_AGENT_ENROLL_HOSTNAME": "same.test.example", "DGX_AGENT_HOSTNAME": "agents.test.example"},
        {"DGX_CONTROL_HOSTNAME": "same.test.example", "DGX_AGENT_ENROLL_HOSTNAME": "enroll.test.example", "DGX_AGENT_HOSTNAME": "same.test.example"},
        {"DGX_CONTROL_HOSTNAME": "control.test.example", "DGX_AGENT_ENROLL_HOSTNAME": "same.test.example", "DGX_AGENT_HOSTNAME": "same.test.example"},
    ):
        result = _entrypoint_result(duplicate)
        assert result.returncode != 0
        assert "must be distinct" in result.stderr

    for equivalent in (
        {"DGX_CONTROL_HOSTNAME": "CONTROL.test.example", "DGX_AGENT_ENROLL_HOSTNAME": "control.test.example.", "DGX_AGENT_HOSTNAME": "agents.test.example"},
        {"DGX_CONTROL_HOSTNAME": "control.test.example", "DGX_AGENT_ENROLL_HOSTNAME": "ENROLL.test.example", "DGX_AGENT_HOSTNAME": "enroll.test.example."},
    ):
        result = _entrypoint_result(equivalent)
        assert result.returncode != 0
        assert "must be distinct" in result.stderr

    malformed = _entrypoint_result({
        "DGX_CONTROL_HOSTNAME": "control test.example",
        "DGX_AGENT_ENROLL_HOSTNAME": "enroll.test.example",
        "DGX_AGENT_HOSTNAME": "agents.test.example",
    })
    assert malformed.returncode != 0
    assert "invalid" in malformed.stderr

    valid = {"DGX_CONTROL_HOSTNAME": "control.test.example", "DGX_AGENT_ENROLL_HOSTNAME": "enroll.test.example", "DGX_AGENT_HOSTNAME": "agents.test.example"}
    for result in (_entrypoint_result(valid), _entrypoint_result(valid, "/dev/null")):
        assert result.returncode != 0
        assert "proxy authentication secret" in result.stderr

    short_secret = tmp_path / "agent-proxy-auth"
    short_secret.write_text("short-secret")
    result = _entrypoint_result(valid, str(short_secret))
    assert result.returncode != 0
    assert "base64url-like" in result.stderr


def test_caddy_proxy_auth_is_one_canonical_base64url_like_line(tmp_path: Path) -> None:
    environment = {
        "DGX_CONTROL_HOSTNAME": "control.test.example",
        "DGX_AGENT_ENROLL_HOSTNAME": "enroll.test.example",
        "DGX_AGENT_HOSTNAME": "agents.test.example",
    }
    token = "A" * 30 + "_-"
    valid_secret = tmp_path / "valid-agent-proxy-auth"
    valid_secret.write_bytes(token.encode("ascii") + b"\r\n")
    result = _entrypoint_result(
        environment,
        str(valid_secret),
        ("/bin/sh", "-c", 'printf "%s" "$DGX_AGENT_PROXY_AUTH"'),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == token

    invalid_values = (
        b"a" * 31 + b"\n",
        b"a" * 32 + b" ",
        b"a" * 16 + b"!" + b"a" * 16,
        b"a" * 16 + b"\n" + b"a" * 16,
        b"a" * 16 + b"\x00" + b"a" * 16,
    )
    for index, value in enumerate(invalid_values):
        invalid_secret = tmp_path / f"invalid-agent-proxy-auth-{index}"
        invalid_secret.write_bytes(value)
        result = _entrypoint_result(environment, str(invalid_secret))
        assert result.returncode != 0
        assert "base64url-like" in result.stderr


def test_rendered_production_boundary_has_only_caddy_public_and_step_ca_private() -> None:
    rendered = _rendered()
    services = rendered["services"]
    assert {name for name, service in services.items() if service.get("ports")} == {"caddy"}
    assert set(services["caddy"]["networks"]) == {"agent-proxy", "ingress", "registry-edge"}
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
    assert "agent-ca-credential" not in builtin_secrets
    assert builtin["services"]["control-api"]["environment"]["DGX_AGENT_CA_PROVIDER"] == "builtin"
    assert builtin["services"]["control-api"]["environment"]["DGX_AGENT_BUILTIN_CA_BOOTSTRAP"] == "1"
    assert "DGX_AGENT_CA_CREDENTIAL_FILE" not in builtin["services"]["control-api"]["environment"]


def test_provider_overlays_require_only_their_own_secrets() -> None:
    base = _rendered("compose.yaml")
    assert "step-ca" not in base["services"]
    assert "DGX_AGENT_CA_PROVIDER" not in base["services"]["control-api"]["environment"]

    builtin_environment = _environment()
    for name in ("AGENT_CA_CREDENTIAL_FILE", "STEP_CA_ROOT_CERTIFICATE_FILE", "STEP_CA_INTERMEDIATE_KEY_FILE", "STEP_CA_PASSWORD_FILE"):
        builtin_environment.pop(name)
    builtin = _rendered("compose.yaml", "compose.builtin-ca.yaml", environment=builtin_environment)
    assert "agent-ca-credential" not in {secret["source"] for secret in builtin["services"]["control-api"]["secrets"]}

    missing_step_secret = _environment()
    missing_step_secret.pop("STEP_CA_PASSWORD_FILE")
    command = ["docker", "compose", "-f", str(ROOT / "deploy/compose/compose.yaml"), "-f", str(ROOT / "deploy/compose/compose.step-ca.yaml"), "config", "--quiet"]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=missing_step_secret,
        check=False,
    )
    assert result.returncode != 0
    assert "STEP_CA_PASSWORD_FILE" in result.stderr


def test_provider_overlays_are_mutually_exclusive_at_application_startup(tmp_path: Path) -> None:
    for overlays in (
        ("compose.step-ca.yaml", "compose.builtin-ca.yaml"),
        ("compose.builtin-ca.yaml", "compose.step-ca.yaml"),
    ):
        rendered = _rendered("compose.yaml", *overlays)
        result = _settings_result(rendered, tmp_path / overlays[0])
        assert result.returncode != 0
        assert "CA provider settings cannot be combined" in result.stderr


def test_each_provider_overlay_passes_application_settings_guard(tmp_path: Path) -> None:
    token = "A" * 30 + "_-"
    for overlay, provider in (
        ("compose.step-ca.yaml", "step-ca"),
        ("compose.builtin-ca.yaml", "builtin"),
    ):
        rendered = _rendered("compose.yaml", overlay)
        result = _settings_result(rendered, tmp_path / provider)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [provider, token]
