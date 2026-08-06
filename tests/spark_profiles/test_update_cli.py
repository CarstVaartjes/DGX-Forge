from __future__ import annotations

import json
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO

import pytest

from spark_profiles.cli import main
from spark_profiles.control_client import (
    ControlClient,
    ControlClientError,
    ControlMalformedResponse,
    UpdatePlanResponse,
    UpdateRolloutResponse,
    UpdateSkewResponse,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
TARGET_SHA = "7" * 64
TARGET_NAME = f"platform/releases/2.0.0/{TARGET_SHA}.json"
NODE = "spk_0123456789abcdef0123456789abcdef"
ROLLOUT_ID = "11111111-1111-4111-8111-111111111111"


@dataclass(frozen=True)
class Document:
    value: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return self.value


SKEW = {
    "affected_nodes": [NODE],
    "digest": f"sha256:{SHA_C}",
    "incompatible_nodes": [],
    "nodes": [
        {
            "active_routes": ["chat"],
            "active_slot": "A",
            "active_workloads": ["model-a"],
            "build_digest": f"sha256:{SHA_B}",
            "compatible": True,
            "display_name": "Alpha Spark",
            "node_id": NODE,
            "platform_version": "1.0.0",
            "protocol_version": 1,
            "reasons": ["control-release-newer"],
            "rollback_slot": "B",
            "status": "update-available",
            "update_required": True,
        }
    ],
    "offline_pending": [],
    "prompt_required": True,
    "target": {
        "build_digest": f"sha256:{SHA_A}",
        "platform_version": "2.0.0",
        "protocol_maximum": 2,
        "protocol_minimum": 1,
        "release": TARGET_NAME,
        "release_digest": f"sha256:{TARGET_SHA}",
        "target_sha256": TARGET_SHA,
        "tuf_targets_version": 7,
    },
}

PLAN = {
    "affected_routes": ["chat"],
    "batches": [[NODE]],
    "canary_node": NODE,
    "gates": [],
    "incompatible": [],
    "offline_pending": [],
    "plan_digest": f"sha256:{SHA_C}",
    "rollback_slots": {NODE: "B"},
    "soak_seconds": 300,
    "target": SKEW["target"],
    "workloads": [
        {"members": [NODE], "minimum_available": 0, "workload_id": "model-a"}
    ],
}

ROLLOUT = {
    "batches": [[NODE]],
    "can_approve_resume": False,
    "current_batch": 0,
    "failure_reason": None,
    "id": ROLLOUT_ID,
    "job_id": "22222222-2222-4222-8222-222222222222",
    "nodes": [{"node_id": NODE, "state": "pending"}],
    "plan_digest": f"sha256:{SHA_C}",
    "required_action": None,
    "resume_required": False,
    "state": "planned",
}


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def update_skew(self) -> Document:
        self.calls.append(("skew",))
        return Document(SKEW)

    def plan_update(self, release: str) -> Document:
        self.calls.append(("plan", release))
        return Document(PLAN)

    def apply_update(self, plan_digest: str) -> Document:
        self.calls.append(("apply", plan_digest))
        return Document(ROLLOUT)

    def update_status(self, rollout_id: str) -> Document:
        self.calls.append(("status", rollout_id))
        return Document(ROLLOUT)


def invoke(client: object, *arguments: str) -> tuple[int, object, str]:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        result = main(arguments, control_client=client)
    return result, json.loads(stdout.getvalue()), stderr.getvalue()


@pytest.mark.parametrize(
    ("arguments", "expected", "payload"),
    [
        (("admin", "updates", "skew", "--json"), ("skew",), SKEW),
        (
            (
                "admin",
                "updates",
                "plan",
                "--release",
                TARGET_NAME,
                "--json",
            ),
            ("plan", TARGET_NAME),
            PLAN,
        ),
        (
            (
                "admin",
                "updates",
                "apply",
                "--plan-digest",
                f"sha256:{SHA_C}",
                "--json",
            ),
            ("apply", f"sha256:{SHA_C}"),
            ROLLOUT,
        ),
        (
            ("admin", "updates", "status", ROLLOUT_ID, "--json"),
            ("status", ROLLOUT_ID),
            ROLLOUT,
        ),
    ],
)
def test_update_commands_are_thin_equivalent_adapters(
    arguments: tuple[str, ...], expected: tuple[object, ...], payload: object
) -> None:
    # Break caught: the CLI locally plans, renames the design-specified command,
    # or sends anything other than the operator's exact release/digest/ID.
    client = FakeClient()

    result, output, stderr = invoke(client, *arguments)

    assert result == 0
    assert output == payload
    assert client.calls == [expected]
    assert stderr == ""


class Response:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._content = json.dumps(payload).encode()
        self.status = status
        self.headers = {"content-type": "application/json"}

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            return self._content
        value, self._content = self._content[:size], self._content[size:]
        return value

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_control_client_uses_exact_update_routes_and_typed_documents(tmp_path) -> None:
    # Break caught: CLI and browser update clients drift onto different routes
    # or drop the exact release/plan digest at their shared API boundary.
    token = tmp_path / "token"
    token.write_text("signed-token\n")
    token.chmod(0o600)
    responses = iter(
        [Response(SKEW), Response(PLAN), Response(ROLLOUT, 202), Response(ROLLOUT)]
    )
    calls: list[urllib.request.Request] = []

    def opener(request: urllib.request.Request, timeout: float) -> Response:
        calls.append(request)
        return next(responses)

    client = ControlClient("https://control.invalid", token, opener=opener)

    skew_result = client.update_skew()
    plan_result = client.plan_update(TARGET_NAME)
    apply_result = client.apply_update(f"sha256:{SHA_C}")
    status_result = client.update_status(ROLLOUT_ID)

    assert isinstance(skew_result, UpdateSkewResponse)
    assert isinstance(plan_result, UpdatePlanResponse)
    assert isinstance(apply_result, UpdateRolloutResponse)
    assert isinstance(status_result, UpdateRolloutResponse)
    assert skew_result.to_dict() == SKEW
    assert plan_result.to_dict() == PLAN
    assert apply_result.to_dict() == ROLLOUT
    assert [(call.method, call.full_url) for call in calls] == [
        ("GET", "https://control.invalid/api/v1/updates/skew"),
        ("POST", "https://control.invalid/api/v1/updates/plan"),
        ("POST", "https://control.invalid/api/v1/updates"),
        ("GET", f"https://control.invalid/api/v1/updates/{ROLLOUT_ID}"),
    ]
    assert json.loads(calls[1].data) == {"release": TARGET_NAME}
    assert json.loads(calls[2].data) == {"plan_digest": f"sha256:{SHA_C}"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_sha256", "8" * 64),
        ("target_sha256", f"sha256:{TARGET_SHA}"),
        ("release_digest", f"sha256:{'8' * 64}"),
        ("tuf_targets_version", 0),
        ("tuf_targets_version", True),
        ("release", f"platform/releases/2.0.0/{'8' * 64}.json"),
        ("release", f"platform/releases/2.0.1/{TARGET_SHA}.json"),
    ],
)
def test_control_client_rejects_inconsistent_tuf_target_identity(
    tmp_path, field: str, value: object
) -> None:
    # Break caught: the UI/CLI displays or applies a target projection whose
    # name, raw target hash, metadata version, and platform version do not bind.
    token = tmp_path / "token"
    token.write_text("signed-token\n")
    token.chmod(0o600)
    candidate = json.loads(json.dumps(SKEW))
    candidate["target"][field] = value

    client = ControlClient(
        "https://control.invalid",
        token,
        opener=lambda request, timeout: Response(candidate),
    )

    with pytest.raises(ControlMalformedResponse):
        client.update_skew()


@pytest.mark.parametrize("required_action", ["rollback", "", 1, False])
def test_control_client_rejects_unknown_update_recovery_action(
    tmp_path, required_action: object
) -> None:
    token = tmp_path / "token"
    token.write_text("signed-token\n")
    token.chmod(0o600)
    candidate = json.loads(json.dumps(ROLLOUT))
    candidate["required_action"] = required_action
    client = ControlClient(
        "https://control.invalid",
        token,
        opener=lambda request, timeout: Response(candidate),
    )

    with pytest.raises(ControlMalformedResponse):
        client.update_status(ROLLOUT_ID)


@pytest.mark.parametrize(
    "digest",
    [SHA_C, f"sha256:{'A' * 64}", "sha256:short", "sha512:" + SHA_C],
)
def test_update_apply_rejects_noncanonical_digest_without_mutation(digest: str) -> None:
    # Break caught: an ambiguous or malformed digest reaches the mutation API.
    client = FakeClient()

    result, output, _ = invoke(
        client,
        "admin",
        "updates",
        "apply",
        "--plan-digest",
        digest,
        "--json",
    )

    assert result == 2
    assert output["error_type"] in {"arguments", "control_api"}
    assert client.calls == []


def test_skew_command_never_applies_an_available_update() -> None:
    # Break caught: reading a NAS-newer prompt accidentally triggers fan-out.
    client = FakeClient()

    result, output, _ = invoke(client, "admin", "updates", "skew", "--json")

    assert result == 0
    assert output["prompt_required"] is True
    assert client.calls == [("skew",)]


@pytest.mark.parametrize(
    "release",
    [
        "platform-release.json",
        f"platform/releases/02.0.0/{TARGET_SHA}.json",
        f"platform/releases/2.0.0/{'A' * 64}.json",
        "../" + "x" * 600,
    ],
)
def test_update_plan_rejects_noncanonical_immutable_release_name(
    release: str,
) -> None:
    # Break caught: a mutable alias, noncanonical semver, digest case mismatch,
    # or traversal-like release target is sent.
    client = FakeClient()

    result, output, _ = invoke(
        client,
        "admin",
        "updates",
        "plan",
        "--release",
        release,
        "--json",
    )

    assert result == 2
    assert output["error_type"] in {"arguments", "control_api"}
    assert client.calls == []


def test_update_client_failures_use_the_bounded_control_error_contract() -> None:
    class Failing(FakeClient):
        def update_skew(self) -> Document:
            raise ControlClientError("token=do-not-render " + "x" * 5000)

    result, output, stderr = invoke(Failing(), "admin", "updates", "skew", "--json")

    assert result == 2
    assert output["error_type"] == "control_api"
    assert "do-not-render" not in json.dumps(output)
    assert len(json.dumps(output)) < 2000
    assert stderr == ""
