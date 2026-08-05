from __future__ import annotations

import json
import os
import ssl
import subprocess
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from threading import Thread
from typing import Any, Self

import pytest

from spark_profiles import backend, cli, health, switcher
from spark_profiles.control_client import ControlClient

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REAL_SUBPROCESS_RUN = subprocess.run
COMMIT = "a" * 40
PLAN_DIGEST = "b" * 64
JOB_ID = "11111111-1111-4111-8111-111111111111"
RECONCILIATION_ID = "22222222-2222-4222-8222-222222222222"
REQUEST_ID = "33333333-3333-4333-8333-333333333333"

PLAN = {
    "agent_protocol_range": [1, 2],
    "commit": COMMIT,
    "digest": PLAN_DIGEST,
    "input_digests": {"profile": "c" * 64},
    "operation_graph": {
        "base_commit": COMMIT,
        "nodes": [],
        "schema_version": 1,
        "targets": ["spk_0000000000000000000000000000000a"],
    },
    "placements": {"model-a": ["spk_0000000000000000000000000000000a"]},
    "reconciliation_id": RECONCILIATION_ID,
    "releases": {},
    "routes": {},
    "targets": ["spk_0000000000000000000000000000000a"],
}
ACCEPTED = {
    "base_commit": COMMIT,
    "job_id": JOB_ID,
    "reconciliation_id": RECONCILIATION_ID,
    "state": "queued",
}
JOB = {
    "base_commit": COMMIT,
    "current_attempt": 1,
    "id": JOB_ID,
    "kind": "reconcile",
    "operations": [],
    "progress": {"completed": 1, "failed": 0, "running": 0, "total": 1},
    "reconciliation_id": RECONCILIATION_ID,
    "state": "succeeded",
    "status_reason": None,
    "targets": ["spk_0000000000000000000000000000000a"],
}
NODES = {
    "commit": COMMIT,
    "nodes": [
        {
            "agent_last_seen_at": "2026-08-05T12:00:00Z",
            "agent_online": True,
            "agent_state": "active",
            "certificate_expires_at": "2026-08-06T12:00:00Z",
            "certificate_expiry_seconds": 86400.0,
            "compatibility": "compatible",
            "disk_available_bytes": 2000000000000,
            "display_name": "token=compute-a",
            "healthy": True,
            "hostname": "compute-a.invalid",
            "id": "spk_0000000000000000000000000000000a",
            "labels": {"pool": "inference"},
            "last_seen_age_seconds": 1.5,
            "last_seen_at": "2026-08-05T12:00:00Z",
            "lifecycle": "ready",
            "memory_available_bytes": 90000000000,
            "probe_age_seconds": 1.5,
            "profile": "profile-a",
            "stale": False,
        }
    ],
}
ENDPOINT = {
    "alias": "model-a",
    "api_base": "https://inference.invalid/v1",
    "expires_at": "2026-08-05T13:00:00Z",
    "generation": 7,
    "node_id": "spk_0000000000000000000000000000000a",
    "observed_at": "2026-08-05T12:00:00Z",
    "plan_digest": PLAN_DIGEST,
    "state": "published",
}


@dataclass(frozen=True)
class ExpectedResponse:
    status: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    body: object
    request_id: str | None


class ApiFixture:
    def __init__(self, responses: list[ExpectedResponse]) -> None:
        self.responses = iter(responses)
        self.requests: list[RecordedRequest] = []


@contextmanager
def control_server(
    *responses: ExpectedResponse,
    tls_context: ssl.SSLContext | None = None,
) -> Iterator[tuple[ApiFixture, str]]:
    fixture = ApiFixture(list(responses))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._handle()

        def do_POST(self) -> None:
            self._handle()

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            fixture.requests.append(
                RecordedRequest(
                    method=self.command,
                    path=self.path,
                    body=json.loads(body) if body else None,
                    request_id=self.headers.get("X-Request-ID"),
                )
            )
            response = next(fixture.responses)
            encoded = json.dumps(response.payload).encode()
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    if tls_context is not None:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        scheme = "https" if tls_context is not None else "http"
        host = "localhost" if tls_context is not None else "127.0.0.1"
        yield fixture, f"{scheme}://{host}:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def api_client(tmp_path: Path, server_url: str) -> ControlClient:
    token = tmp_path / "token"
    token.write_text("test-control-token\n")
    token.chmod(0o600)

    def opener(request: urllib.request.Request, timeout: float):
        translated = urllib.request.Request(
            server_url + request.selector,
            data=request.data,
            headers=dict(request.header_items()),
            method=request.method,
        )
        try:
            response = urllib.request.urlopen(translated, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error

        class LiveResponse:
            def __init__(self) -> None:
                self.status = response.status
                self.headers = dict(response.headers.items())

            def read(self, size: int = -1) -> bytes:
                return response.read(size)

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *args: object) -> None:
                response.close()

        return LiveResponse()

    return ControlClient("https://control.invalid", token, opener=opener)


def invoke(client: ControlClient, *argv: str) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        result = cli.main(
            argv, control_client=client, request_id_factory=lambda: REQUEST_ID
        )
    return result, stdout.getvalue(), stderr.getvalue()


def local_tls_context(tmp_path: Path) -> tuple[ssl.SSLContext, Path]:
    certificate = tmp_path / "control.crt"
    private_key = tmp_path / "control.key"
    REAL_SUBPROCESS_RUN(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, private_key)
    return context, certificate


@pytest.fixture(autouse=True)
def deny_local_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    def rejected(*args: object, **kwargs: object) -> None:
        raise AssertionError("routine sparkctl attempted a local or SSH fallback")

    monkeypatch.setattr(cli, "build_dependencies", rejected, raising=False)
    monkeypatch.setattr(backend.SshBackend, "__init__", rejected)
    monkeypatch.setattr(backend.SshBackend, "from_fleet", rejected)
    monkeypatch.setattr(health.NodeHealthService, "from_repository", rejected)
    monkeypatch.setattr(switcher.ProfileSwitcher, "__init__", rejected)
    monkeypatch.setattr(subprocess, "run", rejected)


def test_documented_console_command_runs_from_outside_project_environment(
    tmp_path: Path,
) -> None:
    # Break caught: `uv sync` installs dependencies but no `sparkctl` console
    # command, so the documented operator journey fails before bounded handling.
    tls_context, certificate = local_tls_context(tmp_path)
    token = tmp_path / "control.token"
    token.write_text("test-control-token\n")
    token.chmod(0o600)
    ssh_marker = tmp_path / "ssh-invoked"
    fake_ssh = tmp_path / "ssh-must-not-run"
    fake_ssh.write_text('#!/bin/sh\nprintf invoked > "$SPARKCTL_SSH_MARKER"\nexit 99\n')
    fake_ssh.chmod(0o755)
    environment = os.environ.copy()
    environment.pop("VIRTUAL_ENV", None)
    environment.pop("PYTHONPATH", None)
    environment.update(
        {
            "DGX_CONTROL_URL": "placeholder",
            "DGX_CONTROL_TOKEN_FILE": str(token),
            "SPARKCTL_SSH_MARKER": str(ssh_marker),
            "SPARK_SSH_BIN": str(fake_ssh),
            "SSL_CERT_FILE": str(certificate),
        }
    )
    with control_server(ExpectedResponse(200, NODES), tls_context=tls_context) as (
        server,
        url,
    ):
        environment["DGX_CONTROL_URL"] = url
        completed = REAL_SUBPROCESS_RUN(
            [
                "uv",
                "run",
                "--project",
                str(REPOSITORY_ROOT),
                "sparkctl",
                "nodes",
                "status",
                "--json",
            ],
            cwd=tmp_path,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == NODES
    assert completed.stderr == ""
    assert server.requests == [
        RecordedRequest("GET", "/api/v1/nodes/status", None, None)
    ]
    assert not ssh_marker.exists()


@pytest.mark.parametrize(
    ("argv", "response", "method", "path", "body"),
    [
        (("nodes", "status", "--json"), NODES, "GET", "/api/v1/nodes/status", None),
        (
            ("validate", "profile-a", "--json"),
            PLAN,
            "POST",
            "/api/v1/profiles/profile-a/plan",
            None,
        ),
        (
            ("prepare", "profile-a", "--json"),
            PLAN,
            "POST",
            "/api/v1/profiles/profile-a/plan",
            None,
        ),
        (
            ("switch", "profile-a", "--json"),
            PLAN,
            "POST",
            "/api/v1/profiles/profile-a/plan",
            None,
        ),
        (
            ("restore-default", "--json"),
            PLAN,
            "POST",
            "/api/v1/profiles/default/plan",
            None,
        ),
        (
            ("endpoint", "model-a", "--json"),
            ENDPOINT,
            "GET",
            "/api/v1/endpoints/model-a",
            None,
        ),
    ],
)
def test_routine_commands_emit_exact_server_models_without_local_dependencies(
    tmp_path: Path,
    argv: tuple[str, ...],
    response: dict[str, Any],
    method: str,
    path: str,
    body: object,
) -> None:
    # Break caught: a routine command uses local state/SSH, the wrong API route,
    # or reshapes data instead of returning the generated server model.
    with control_server(ExpectedResponse(200, response)) as (server, url):
        result, stdout, stderr = invoke(api_client(tmp_path, url), *argv)

    assert result == 0
    assert json.loads(stdout) == response
    assert stderr == ""
    assert server.requests == [RecordedRequest(method, path, body, None)]


def test_plan_output_preserves_every_server_target_past_sixty_four(
    tmp_path: Path,
) -> None:
    # Break caught: successful generated-model arrays are silently truncated by
    # the bounded error sanitizer before an operator reviews the server digest.
    targets = [f"spk_{index:032x}" for index in range(70)]
    operation_graph = {**PLAN["operation_graph"], "targets": targets}
    plan = {**PLAN, "operation_graph": operation_graph, "targets": targets}
    with control_server(ExpectedResponse(200, plan)) as (_, url):
        result, stdout, stderr = invoke(
            api_client(tmp_path, url), "validate", "profile-a", "--json"
        )

    assert result == 0
    assert json.loads(stdout) == plan
    assert stderr == ""


@pytest.mark.parametrize(
    "command",
    [
        ("prepare", "profile-a"),
        ("switch", "profile-a"),
        ("restore-default",),
    ],
)
def test_apply_uses_server_digest_and_no_wait_returns_accepted_job(
    tmp_path: Path, command: tuple[str, ...]
) -> None:
    # Break caught: mutation occurs without the exact freshly planned digest,
    # reuses an arbitrary request ID, or --no-wait performs a job GET.
    with control_server(
        ExpectedResponse(200, PLAN), ExpectedResponse(202, ACCEPTED)
    ) as (server, url):
        result, stdout, stderr = invoke(
            api_client(tmp_path, url), *command, "--apply", "--no-wait", "--json"
        )

    assert result == 0
    assert json.loads(stdout) == ACCEPTED
    assert stderr == ""
    profile = "default" if command == ("restore-default",) else "profile-a"
    assert server.requests == [
        RecordedRequest("POST", f"/api/v1/profiles/{profile}/plan", None, None),
        RecordedRequest(
            "POST",
            "/api/v1/reconciliations",
            {"plan_digest": PLAN_DIGEST},
            REQUEST_ID,
        ),
    ]


@pytest.mark.parametrize(
    "command",
    [
        ("prepare", "profile-a"),
        ("switch", "profile-a"),
        ("restore-default",),
    ],
)
@pytest.mark.parametrize("wait_arguments", [(), ("--wait",)])
def test_apply_waits_by_default_and_wait_flag_is_explicitly_supported(
    tmp_path: Path,
    command: tuple[str, ...],
    wait_arguments: tuple[str, ...],
) -> None:
    # Break caught: synchronous compatibility returns before terminal state or
    # polls by any route other than the accepted job's GET resource.
    with control_server(
        ExpectedResponse(200, PLAN),
        ExpectedResponse(202, ACCEPTED),
        ExpectedResponse(200, JOB),
    ) as (server, url):
        result, stdout, stderr = invoke(
            api_client(tmp_path, url),
            *command,
            "--apply",
            *wait_arguments,
            "--json",
        )

    assert result == 0
    assert json.loads(stdout) == JOB
    assert stderr == ""
    assert server.requests[-1] == RecordedRequest(
        "GET", f"/api/v1/jobs/{JOB_ID}", None, None
    )


@pytest.mark.parametrize(
    "argv",
    [
        ("nodes", "status", "--json"),
        ("validate", "profile-a", "--json"),
        ("prepare", "profile-a", "--json"),
        ("switch", "profile-a", "--json"),
        ("restore-default", "--json"),
        ("endpoint", "model-a", "--json"),
    ],
)
def test_control_unavailability_is_bounded_secret_free_and_never_falls_back(
    tmp_path: Path, argv: tuple[str, ...]
) -> None:
    # Break caught: a control error selects legacy/local execution or discloses
    # server detail/token material instead of one stable bounded error object.
    detail = "Authorization: Bearer server-secret " + "x" * 5000
    with control_server(ExpectedResponse(503, {"detail": detail})) as (_, url):
        result, stdout, stderr = invoke(api_client(tmp_path, url), *argv)

    assert result == 2
    assert stderr == ""
    assert len(stdout) < 2000
    assert "server-secret" not in stdout
    assert json.loads(stdout) == {
        "error": "control API unavailable",
        "error_type": "control_api",
    }


@pytest.mark.parametrize(
    ("responses", "arguments", "expected_requests", "expected_error"),
    [
        (
            (ExpectedResponse(200, PLAN), ExpectedResponse(503, {"detail": "busy"})),
            ("--no-wait",),
            2,
            "control API unavailable",
        ),
        (
            (
                ExpectedResponse(200, PLAN),
                ExpectedResponse(202, ACCEPTED),
                ExpectedResponse(404, {"detail": "job not found"}),
            ),
            ("--wait",),
            3,
            "control API returned HTTP 404: job not found",
        ),
    ],
)
def test_apply_and_poll_failures_never_replay_or_select_legacy(
    tmp_path: Path,
    responses: tuple[ExpectedResponse, ...],
    arguments: tuple[str, ...],
    expected_requests: int,
    expected_error: str,
) -> None:
    # Break caught: a post-plan apply/poll failure retries a mutation or changes
    # transport to the separately named legacy controller.
    with control_server(*responses) as (server, url):
        result, stdout, stderr = invoke(
            api_client(tmp_path, url),
            "switch",
            "profile-a",
            "--apply",
            *arguments,
            "--json",
        )

    assert result == 2
    assert json.loads(stdout) == {
        "error": expected_error,
        "error_type": "control_api",
    }
    assert stderr == ""
    assert len(server.requests) == expected_requests
    assert (
        sum(request.path == "/api/v1/reconciliations" for request in server.requests)
        == 1
    )


def test_legacy_mode_cannot_be_selected_through_the_standard_cli() -> None:
    # Break caught: recovery compatibility becomes an implicit or ordinary
    # production command rather than a separately named launcher.
    stdout = StringIO()
    with redirect_stdout(stdout):
        result = cli.main(("--json", "legacy", "status"))

    assert result == 2
    assert json.loads(stdout.getvalue())["error_type"] == "arguments"
