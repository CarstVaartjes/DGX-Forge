import io
import json
import time
import urllib.error
from pathlib import Path

import pytest

from spark_profiles import control_client
from spark_profiles.cli import main
from spark_profiles.control_client import ControlClient, ControlClientError
from spark_profiles.generated_control.models.agents_response import AgentsResponse
from spark_profiles.generated_control.models.endpoint_response import EndpointResponse
from spark_profiles.generated_control.models.fleet_status_response import (
    FleetStatusResponse,
)
from spark_profiles.generated_control.models.job_detail_response import (
    JobDetailResponse,
)
from spark_profiles.generated_control.models.reconciliation_accepted_response import (
    ReconciliationAcceptedResponse,
)
from spark_profiles.generated_control.models.reconciliation_plan_response import (
    ReconciliationPlanResponse,
)

COMMIT = "a" * 40
PLAN_DIGEST = "b" * 64
JOB_ID = "11111111-1111-4111-8111-111111111111"


class Response:
    def __init__(self, payload, status=200, *, headers=None, raw=False):
        self._content = payload if raw else json.dumps(payload).encode()
        self.status = status
        self.headers = {"content-type": "application/json", **(headers or {})}

    def read(self, size=-1):
        if size < 0:
            return self._content
        value, self._content = self._content[:size], self._content[size:]
        return value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def token_file(tmp_path: Path) -> Path:
    token = tmp_path / "token"
    token.write_text("signed-token\n")
    token.chmod(0o600)
    return token


def plan_payload() -> dict[str, object]:
    return {
        "agent_protocol_range": [1, 1],
        "commit": COMMIT,
        "digest": PLAN_DIGEST,
        "input_digests": {},
        "operation_graph": {
            "base_commit": COMMIT,
            "nodes": [],
            "schema_version": 1,
            "targets": [],
        },
        "placements": {},
        "reconciliation_id": "22222222-2222-4222-8222-222222222222",
        "releases": {},
        "routes": {},
        "targets": [],
    }


def job_payload(state: str, reason: str | None = None) -> dict[str, object]:
    return {
        "base_commit": COMMIT,
        "current_attempt": 1,
        "id": JOB_ID,
        "kind": "reconcile",
        "operations": [],
        "progress": {"completed": 0, "failed": 0, "running": 0, "total": 0},
        "reconciliation_id": "22222222-2222-4222-8222-222222222222",
        "state": state,
        "status_reason": reason,
        "targets": [],
    }


def test_client_reads_token_file_and_sends_canonical_proposal(tmp_path: Path) -> None:
    token = token_file(tmp_path)
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        return Response({"digest": "abc", "patch": "diff"})

    client = ControlClient("https://control.invalid", token, opener=opener)
    result = client.create_proposal(
        {
            "base_commit": "base",
            "changes": [
                {"path": "inventory/fleet.toml", "document": {"schema_version": 2}}
            ],
        }
    )
    request = calls[0][0]
    assert request.full_url == "https://control.invalid/api/v1/proposals"
    assert request.headers["Authorization"] == "Bearer signed-token"
    assert json.loads(request.data) == {
        "base_commit": "base",
        "changes": [
            {"document": {"schema_version": 2}, "path": "inventory/fleet.toml"}
        ],
    }
    assert result == {"digest": "abc", "patch": "diff"}


def test_operational_methods_use_generated_models_and_exact_routes(
    tmp_path: Path,
) -> None:
    responses = iter(
        [
            Response({"commit": COMMIT, "nodes": []}),
            Response(plan_payload()),
            Response(
                {
                    "base_commit": COMMIT,
                    "job_id": JOB_ID,
                    "reconciliation_id": "22222222-2222-4222-8222-222222222222",
                    "state": "queued",
                },
                status=202,
            ),
            Response(job_payload("running")),
            Response(
                {
                    "alias": "model-a",
                    "api_base": "https://model.invalid/v1",
                    "expires_at": "2026-08-05T13:00:00Z",
                    "generation": 7,
                    "node_id": "spk_" + "c" * 32,
                    "observed_at": "2026-08-05T12:00:00Z",
                    "plan_digest": PLAN_DIGEST,
                    "state": "active",
                }
            ),
            Response({"agents": []}),
        ]
    )
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        return next(responses)

    client = ControlClient(
        "https://control.invalid", token_file(tmp_path), opener=opener
    )

    assert isinstance(client.nodes(), FleetStatusResponse)
    assert isinstance(client.plan_profile("agent"), ReconciliationPlanResponse)
    assert isinstance(
        client.apply_plan(
            PLAN_DIGEST, request_id="33333333-3333-4333-8333-333333333333"
        ),
        ReconciliationAcceptedResponse,
    )
    assert isinstance(client.job(JOB_ID), JobDetailResponse)
    assert isinstance(client.endpoint("model-a"), EndpointResponse)
    assert isinstance(client.agents(), AgentsResponse)

    assert [(call[0].method, call[0].full_url) for call in calls] == [
        ("GET", "https://control.invalid/api/v1/nodes/status"),
        ("POST", "https://control.invalid/api/v1/profiles/agent/plan"),
        ("POST", "https://control.invalid/api/v1/reconciliations"),
        ("GET", f"https://control.invalid/api/v1/jobs/{JOB_ID}"),
        ("GET", "https://control.invalid/api/v1/endpoints/model-a"),
        ("GET", "https://control.invalid/api/v1/agents"),
    ]
    assert calls[2][0].headers["X-request-id"] == (
        "33333333-3333-4333-8333-333333333333"
    )
    assert json.loads(calls[2][0].data) == {"plan_digest": PLAN_DIGEST}


@pytest.mark.parametrize(
    ("status", "exception_name"),
    [
        (401, "ControlUnauthorized"),
        (403, "ControlForbidden"),
        (409, "ControlConflict"),
        (503, "ControlUnavailable"),
    ],
)
def test_control_statuses_raise_typed_failures(
    tmp_path: Path, status: int, exception_name: str
) -> None:
    def opener(request, timeout):
        return Response({"detail": "bounded failure"}, status=status)

    client = ControlClient(
        "https://control.invalid", token_file(tmp_path), opener=opener
    )
    expected = getattr(control_client, exception_name)

    with pytest.raises(expected) as caught:
        client.plan_profile("agent")

    assert caught.value.status_code == status
    assert caught.value.detail == "bounded failure"


def test_missing_resource_raises_typed_not_found(tmp_path: Path) -> None:
    def opener(request, timeout):
        return Response({"detail": "job not found"}, status=404)

    client = ControlClient(
        "https://control.invalid", token_file(tmp_path), opener=opener
    )

    with pytest.raises(control_client.ControlNotFound) as caught:
        client.job(JOB_ID)

    assert caught.value.status_code == 404
    assert caught.value.detail == "job not found"


def test_malformed_json_raises_typed_response_failure(tmp_path: Path) -> None:
    def opener(request, timeout):
        return Response(b"not-json", raw=True)

    client = ControlClient(
        "https://control.invalid", token_file(tmp_path), opener=opener
    )

    with pytest.raises(control_client.ControlMalformedResponse, match="invalid JSON"):
        client.nodes()


def test_malformed_generated_model_raises_typed_response_failure(
    tmp_path: Path,
) -> None:
    def opener(request, timeout):
        return Response({"commit": COMMIT})

    client = ControlClient(
        "https://control.invalid", token_file(tmp_path), opener=opener
    )

    with pytest.raises(control_client.ControlMalformedResponse, match="schema"):
        client.nodes()


def test_oversized_generated_response_is_rejected_before_parsing(
    tmp_path: Path,
) -> None:
    def opener(request, timeout):
        return Response(b"{" + b" " * 1_048_576, raw=True)

    client = ControlClient(
        "https://control.invalid", token_file(tmp_path), opener=opener
    )

    with pytest.raises(control_client.ControlResponseTooLarge, match="safety limit"):
        client.nodes()


def test_generated_response_requires_json_content_type(tmp_path: Path) -> None:
    def opener(request, timeout):
        return Response(
            {"commit": COMMIT, "nodes": []},
            headers={"content-type": "text/html"},
        )

    client = ControlClient(
        "https://control.invalid", token_file(tmp_path), opener=opener
    )

    with pytest.raises(control_client.ControlMalformedResponse, match="content type"):
        client.nodes()


def test_ambiguous_apply_transport_failure_is_not_replayed(
    tmp_path: Path,
) -> None:
    calls = []

    def opener(request, timeout):
        calls.append(request)
        raise urllib.error.URLError("signed-token upstream reset")

    client = ControlClient(
        "https://control.invalid", token_file(tmp_path), opener=opener
    )

    with pytest.raises(control_client.ControlTransportError) as caught:
        client.apply_plan(
            PLAN_DIGEST, request_id="33333333-3333-4333-8333-333333333333"
        )

    assert len(calls) == 1
    assert calls[0].headers["X-request-id"] == ("33333333-3333-4333-8333-333333333333")
    assert "signed-token" not in str(caught.value)


def test_urlopen_http_error_body_keeps_typed_status_mapping(tmp_path: Path) -> None:
    def opener(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            503,
            "Service Unavailable",
            {"content-type": "application/json"},
            io.BytesIO(b'{"detail":"try later"}'),
        )

    client = ControlClient(
        "https://control.invalid", token_file(tmp_path), opener=opener
    )

    with pytest.raises(control_client.ControlUnavailable) as caught:
        client.plan_profile("agent")

    assert caught.value.status_code == 503
    assert caught.value.detail == "try later"


@pytest.mark.parametrize(
    ("header", "expected"),
    [("0", 1), ("1", 1), ("17", 17), ("31", 30), ("invalid", None)],
)
def test_retry_after_is_bounded_to_safe_seconds(
    tmp_path: Path, header: str, expected: int | None
) -> None:
    def opener(request, timeout):
        return Response(
            {"detail": "try later"},
            status=503,
            headers={"retry-after": header},
        )

    client = ControlClient(
        "https://control.invalid", token_file(tmp_path), opener=opener
    )

    with pytest.raises(control_client.ControlUnavailable) as caught:
        client.plan_profile("agent")

    assert caught.value.retry_after_seconds == expected


def test_wait_job_returns_structured_terminal_success(tmp_path: Path) -> None:
    def opener(request, timeout):
        return Response(job_payload("succeeded"))

    client = ControlClient(
        "https://control.invalid", token_file(tmp_path), opener=opener
    )

    result = client.wait_job(JOB_ID, timeout=1, interval=0)

    assert isinstance(result, JobDetailResponse)
    assert result.state == "succeeded"
    assert result.id == JOB_ID


@pytest.mark.parametrize(
    ("state", "exception_name"),
    [
        ("failed", "JobFailed"),
        ("expired", "JobFailed"),
        ("waiting-for-operator", "JobWaitingForOperator"),
    ],
)
def test_wait_job_raises_typed_terminal_failure(
    tmp_path: Path, state: str, exception_name: str
) -> None:
    def opener(request, timeout):
        return Response(job_payload(state, "operator action required"))

    client = ControlClient(
        "https://control.invalid", token_file(tmp_path), opener=opener
    )
    expected = getattr(control_client, exception_name)

    with pytest.raises(expected) as caught:
        client.wait_job(JOB_ID, timeout=1, interval=0)

    assert isinstance(caught.value.job, JobDetailResponse)
    assert caught.value.job.state == state
    assert caught.value.reason == "operator action required"


def test_wait_job_polls_only_get_until_terminal_state(tmp_path: Path) -> None:
    responses = iter(
        [
            Response(job_payload("queued")),
            Response(job_payload("running")),
            Response(job_payload("succeeded")),
        ]
    )
    calls = []

    def opener(request, timeout):
        calls.append(request)
        return next(responses)

    client = ControlClient(
        "https://control.invalid", token_file(tmp_path), opener=opener
    )

    result = client.wait_job(JOB_ID, timeout=1, interval=0)

    assert result.state == "succeeded"
    assert [(request.method, request.full_url) for request in calls] == [
        ("GET", f"https://control.invalid/api/v1/jobs/{JOB_ID}"),
        ("GET", f"https://control.invalid/api/v1/jobs/{JOB_ID}"),
        ("GET", f"https://control.invalid/api/v1/jobs/{JOB_ID}"),
    ]


def test_wait_job_times_out_with_last_observation(tmp_path: Path) -> None:
    calls = 0

    def opener(request, timeout):
        nonlocal calls
        calls += 1
        if calls > 50:
            raise AssertionError("polling ignored its deadline")
        return Response(job_payload("queued"))

    client = ControlClient(
        "https://control.invalid", token_file(tmp_path), opener=opener
    )

    with pytest.raises(control_client.ControlTimeout) as caught:
        client.wait_job(JOB_ID, timeout=0.005, interval=0.001)

    assert caught.value.job is not None
    assert caught.value.job.state == "queued"
    assert 1 <= calls <= 50


def test_wait_job_honors_bounded_retry_after_on_get(tmp_path: Path) -> None:
    responses = iter(
        [
            Response(
                {"detail": "temporarily unavailable"},
                status=503,
                headers={"retry-after": "0"},
            ),
            Response(job_payload("succeeded")),
        ]
    )
    calls = []

    def opener(request, timeout):
        calls.append(request)
        return next(responses)

    client = ControlClient(
        "https://control.invalid", token_file(tmp_path), opener=opener
    )
    started = time.monotonic()

    result = client.wait_job(JOB_ID, timeout=2, interval=0)

    elapsed = time.monotonic() - started
    assert result.state == "succeeded"
    assert 0.9 <= elapsed < 2
    assert [request.method for request in calls] == ["GET", "GET"]


def test_wait_job_retries_ambiguous_get_transport_failure(tmp_path: Path) -> None:
    calls = []

    def opener(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise urllib.error.URLError("connection reset")
        return Response(job_payload("succeeded"))

    client = ControlClient(
        "https://control.invalid", token_file(tmp_path), opener=opener
    )

    result = client.wait_job(JOB_ID, timeout=1, interval=0)

    assert result.state == "succeeded"
    assert [request.method for request in calls] == ["GET", "GET"]


def test_client_rejects_symlink_token(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.write_text("token")
    link = tmp_path / "token"
    link.symlink_to(actual)
    with pytest.raises(ControlClientError, match="non-symlink"):
        ControlClient("https://control.invalid", link)


def test_client_rejects_control_url_with_path(tmp_path: Path) -> None:
    with pytest.raises(ControlClientError, match="HTTPS origin"):
        ControlClient("https://control.invalid/admin", token_file(tmp_path))


def test_client_rejects_group_or_world_readable_token(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("signed-token")
    token.chmod(0o640)

    with pytest.raises(ControlClientError, match="permissions"):
        ControlClient("https://control.invalid", token)


def test_apply_rejects_noncanonical_request_id_before_network(
    tmp_path: Path,
) -> None:
    def opener(request, timeout):
        raise AssertionError("invalid request ID reached the network")

    client = ControlClient(
        "https://control.invalid", token_file(tmp_path), opener=opener
    )

    with pytest.raises(ControlClientError, match="request ID"):
        client.apply_plan(PLAN_DIGEST, request_id="not-a-uuid")


class FakeAdminClient:
    def __init__(self):
        self.payload = None

    def create_proposal(self, payload):
        self.payload = payload
        return {"digest": "same", "patch": "canonical"}


def test_sparkctl_admin_proposal_is_thin_api_adapter(tmp_path: Path, capsys) -> None:
    change = tmp_path / "change.json"
    change.write_text(
        json.dumps(
            {
                "base_commit": "a" * 40,
                "changes": [
                    {"path": "inventory/fleet.toml", "document": {"schema_version": 2}}
                ],
            }
        )
    )
    client = FakeAdminClient()
    assert (
        main(
            ["admin", "proposal", "--file", str(change), "--json"],
            control_client=client,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "digest": "same",
        "patch": "canonical",
    }
    assert client.payload["base_commit"] == "a" * 40
