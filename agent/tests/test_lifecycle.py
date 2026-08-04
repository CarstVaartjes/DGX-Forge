from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from dgx_agent.client import AgentTransportError, CredentialStore, IssuedCredential
from dgx_agent.config import AgentConfig
from dgx_agent.main import (
    Agent,
    ensure_initial_enrollment,
    remove_consumed_enrollment_token,
)
from dgx_agent.operations import OperationContext, OperationRegistry
from dgx_agent.readiness import ReadinessError, ReadinessReporter
from dgx_agent.state import AgentStateStore
from dgx_agent_protocol import AgentClaim, AgentOperation, canonical_message

NODE_ID = "spk_0123456789abcdef0123456789abcdef"


def probe_claim(*, deadline: datetime | None = None) -> AgentClaim:
    payload: dict[str, object] = {}
    return AgentClaim(
        schema_version=1,
        job_id=str(uuid.uuid4()),
        operation_id=str(uuid.uuid4()),
        attempt=1,
        fence=str(uuid.uuid4()),
        node_id=NODE_ID,
        operation=AgentOperation.NODE_PROBE,
        base_commit="a" * 40,
        payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
        payload=payload,
        deadline=deadline or datetime.now(UTC) + timedelta(minutes=1),
    )


class FakeControl:
    def __init__(self) -> None:
        self.claims: list[AgentClaim] = []
        self.results: list[dict[str, object]] = []
        self.claim_calls = 0
        self.result_failures = 0

    def queue(self, claim: AgentClaim) -> None:
        self.claims.append(claim)

    def claim(self) -> AgentClaim | None:
        self.claim_calls += 1
        return self.claims.pop(0) if self.claims else None

    def result(self, result) -> None:
        self.results.append(json.loads(canonical_message(result)))
        if self.result_failures:
            self.result_failures -= 1
            raise AgentTransportError("control plane disconnected")


class Probe:
    def collect(self, deadline: datetime) -> dict[str, object]:
        return {"status": "healthy"}


def test_readiness_reporter_requires_complete_environment_and_publishes_exact_marker(
    tmp_path: Path,
) -> None:
    assert ReadinessReporter._from_environment_for_test({}, tmp_path).report() is False
    with pytest.raises(ReadinessError):
        ReadinessReporter._from_environment_for_test(
            {"DGX_AGENT_SUPERVISOR_GENERATION": "2"}, tmp_path
        )
    environment = {
        "DGX_AGENT_SUPERVISOR_GENERATION": "2",
        "DGX_AGENT_SUPERVISOR_SLOT": "B",
        "DGX_AGENT_SUPERVISOR_SHA256": "a" * 64,
    }
    reporter = ReadinessReporter._from_environment_for_test(environment, tmp_path)

    assert reporter.report() is True
    marker = tmp_path / "readiness.json"
    assert marker.read_bytes() == (
        b'{"generation":2,"schema_version":1,"sha256":"'
        + b"a" * 64
        + b'","slot":"B"}\n'
    )
    assert marker.stat().st_mode & 0o777 == 0o600
    before = marker.stat().st_mtime_ns
    assert reporter.report() is False
    assert marker.stat().st_mtime_ns == before


def test_readiness_callback_runs_only_after_authenticated_runtime_exchange(
    tmp_path: Path,
) -> None:
    context = OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(tmp_path / "state"),
        probe=Probe(),
    )
    calls: list[str] = []
    empty = FakeControl()
    Agent(
        empty,
        OperationRegistry(),
        context,
        on_authenticated_exchange=lambda: calls.append("ready"),
    ).run_once()
    assert calls == ["ready"]

    failing = _SequencedControl([AgentTransportError("offline")])
    with pytest.raises(AgentTransportError):
        Agent(
            failing,
            OperationRegistry(),
            context,
            on_authenticated_exchange=lambda: calls.append("unsafe"),
        ).run_once()
    assert calls == ["ready"]


def test_agent_claims_executes_and_reports_with_same_fence(tmp_path: Path) -> None:
    fake_control = FakeControl()
    claim = probe_claim()
    fake_control.queue(claim)
    state = AgentStateStore(tmp_path / "state")
    context = OperationContext(node_id=NODE_ID, state=state, probe=Probe())
    agent = Agent(fake_control, OperationRegistry(), context)

    agent.run_once()

    assert fake_control.results[0]["fence"] == claim.fence
    assert fake_control.results[0]["state"] == "succeeded"


def test_pending_result_is_replayed_before_any_new_claim(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state")
    context = OperationContext(node_id=NODE_ID, state=state, probe=Probe())
    disconnected = FakeControl()
    disconnected.result_failures = 1
    original = probe_claim()
    disconnected.queue(original)

    with pytest.raises(AgentTransportError):
        Agent(disconnected, OperationRegistry(), context).run_once()

    pending = state.recover_pending()
    assert pending is not None and pending.result is not None
    restarted = FakeControl()
    restarted.queue(probe_claim())
    Agent(restarted, OperationRegistry(), context).run_once()

    assert restarted.claim_calls == 0
    assert restarted.results[0]["fence"] == original.fence
    assert state.recover_pending() is None


def test_active_attempt_is_recovered_and_executed_before_any_new_claim(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state")
    active = probe_claim()
    state.begin(active)
    context = OperationContext(node_id=NODE_ID, state=state, probe=Probe())
    restarted = FakeControl()
    restarted.queue(probe_claim())

    Agent(restarted, OperationRegistry(), context).run_once()

    assert restarted.claim_calls == 0
    assert restarted.results[0]["fence"] == active.fence
    assert state.recover_active() is None
    assert state.recover_pending() is None


def test_expired_active_attempt_persists_and_replays_exact_failure_before_new_claim(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state")
    expired = probe_claim(deadline=datetime.now(UTC) - timedelta(seconds=1))
    state.begin(expired)
    context = OperationContext(node_id=NODE_ID, state=state, probe=Probe())
    disconnected = FakeControl()
    disconnected.result_failures = 1

    with pytest.raises(AgentTransportError):
        Agent(disconnected, OperationRegistry(), context).run_once()

    pending = state.recover_pending()
    assert pending is not None and pending.result is not None
    assert pending.result.fence == expired.fence
    assert pending.result.deadline == expired.deadline
    assert pending.result.state == "failed"
    assert pending.result.result == {
        "status": "failed",
        "error_code": "claim_deadline_expired",
    }
    assert disconnected.claim_calls == 0

    fresh = probe_claim()
    restarted = FakeControl()
    restarted.queue(fresh)
    agent = Agent(restarted, OperationRegistry(), context)

    agent.run_once()

    assert restarted.claim_calls == 0
    assert restarted.results == [json.loads(pending.canonical_result)]
    assert state.recover_pending() is None

    agent.run_once()

    assert restarted.claim_calls == 1
    assert [result["fence"] for result in restarted.results] == [
        expired.fence,
        fresh.fence,
    ]


class _SequencedControl(FakeControl):
    def __init__(self, outcomes: list[object]) -> None:
        super().__init__()
        self._outcomes = outcomes

    def claim(self) -> AgentClaim | None:
        self.claim_calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]


class _Stop:
    def __init__(self, waits_before_stop: int) -> None:
        self.waits_before_stop = waits_before_stop
        self.delays: list[float] = []

    def is_set(self) -> bool:
        return len(self.delays) >= self.waits_before_stop

    def wait(self, delay: float) -> bool:
        self.delays.append(delay)
        return self.is_set()


def test_run_forever_applies_bounded_jitter_and_resets_after_success(
    tmp_path: Path,
) -> None:
    control = _SequencedControl(
        [
            AgentTransportError("first outage"),
            None,
            AgentTransportError("second outage"),
        ]
    )
    stop = _Stop(waits_before_stop=2)
    context = OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(tmp_path / "state"),
        probe=Probe(),
    )
    agent = Agent(
        control,
        OperationRegistry(),
        context,
        backoff_min_seconds=1,
        backoff_max_seconds=3,
        jitter=lambda upper: upper,
    )

    agent.run_forever(stop)

    assert stop.delays == [1, 1]


def _credential_material(tmp_path: Path):
    now = datetime.now(UTC)
    ca_key = ed25519.Ed25519PrivateKey.generate()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "rotation-ca")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, algorithm=None)
    )
    key = ed25519.Ed25519PrivateKey.generate()
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, NODE_ID)]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=2))
        .not_valid_after(now + timedelta(minutes=30))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(
                        f"spiffe://dgx-forge.local/node/{NODE_ID}"
                    )
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
        )
        .sign(ca_key, algorithm=None)
    )

    def write(name: str, value: bytes, mode: int) -> Path:
        path = tmp_path / name
        path.write_bytes(value)
        path.chmod(mode)
        return path

    return (
        write("ca.pem", ca.public_bytes(serialization.Encoding.PEM), 0o644),
        write(
            "client.pem", certificate.public_bytes(serialization.Encoding.PEM), 0o644
        ),
        write(
            "client.key",
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
            0o600,
        ),
        ca,
        ca_key,
    )


def _issue_rotation(
    csr_pem: bytes, ca: x509.Certificate, ca_key, *, generation: int = 2
) -> IssuedCredential:
    request = x509.load_pem_x509_csr(csr_pem)
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(request.subject)
        .issuer_name(ca.subject)
        .public_key(request.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            request.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value,
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
        )
        .sign(ca_key, algorithm=None)
    )
    der = certificate.public_bytes(serialization.Encoding.DER)
    return IssuedCredential(
        node_id=NODE_ID,
        certificate_pem=certificate.public_bytes(serialization.Encoding.PEM),
        chain_pem=ca.public_bytes(serialization.Encoding.PEM),
        serial=str(certificate.serial_number),
        fingerprint=hashlib.sha256(der).hexdigest(),
        not_before=certificate.not_valid_before_utc,
        not_after=certificate.not_valid_after_utc,
        generation=generation,
    )


class EnrollmentControl:
    def __init__(self, response) -> None:
        self.response = response
        self.csrs: list[bytes] = []

    def enroll(self, _origin: str, _token: str, csr: bytes, _evidence):
        self.csrs.append(csr)
        return self.response


def test_initial_enrollment_reuses_csr_keeps_token_until_durable_pickup(
    tmp_path: Path,
) -> None:
    ca_path, _certificate_path, _key_path, ca, ca_key = _credential_material(tmp_path)
    state_root = tmp_path / "bootstrap-state"
    token = tmp_path / "enrollment-token"
    token.write_text("t" * 43 + "\n")
    token.chmod(0o600)
    missing_certificate = tmp_path / "initial-missing.pem"
    missing_key = tmp_path / "initial-missing.key"
    config = AgentConfig(
        "https://runtime.example",
        "https://enroll.example",
        NODE_ID,
        missing_certificate,
        missing_key,
        ca_path,
        1,
        60,
        state_root,
        tmp_path / "nvidia.json",
        tmp_path / "runtime.json",
        token,
    )
    store = CredentialStore(state_root, ca_path, missing_certificate, missing_key)
    from dgx_agent.client import EnrollmentPending

    pending_control = EnrollmentControl(
        EnrollmentPending(
            "00000000-0000-0000-0000-000000000001", NODE_ID, "pending-approval"
        )
    )
    evidence = {
        "agent_digest": "a" * 64,
        "boot_id": "boot",
        "csr_public_key_fingerprint": "b" * 64,
        "hardware_fingerprint": "hardware",
        "host_key_fingerprint": "host",
        "node_id": NODE_ID,
    }

    assert ensure_initial_enrollment(config, store, pending_control, evidence) is False
    assert token.exists()
    issued = _issue_rotation(pending_control.csrs[0], ca, ca_key, generation=1)
    issued_control = EnrollmentControl(issued)

    assert ensure_initial_enrollment(config, store, issued_control, evidence) is True
    assert issued_control.csrs == pending_control.csrs
    assert not token.exists()
    assert store.has_active_credentials

    # A crash after active publication but before unlink is recovered safely.
    token.write_text("t" * 43 + "\n")
    token.chmod(0o600)
    assert remove_consumed_enrollment_token(config, store) is True
    assert not token.exists()


class RotationControl(FakeControl):
    def __init__(
        self,
        issued: IssuedCredential,
        *,
        fail_renew: int = 0,
        fail_activate: int = 0,
        fail_claim: bool = False,
    ) -> None:
        super().__init__()
        self.issued = issued
        self.fail_renew = fail_renew
        self.fail_activate = fail_activate
        self.fail_claim = fail_claim
        self.renewed_csrs: list[bytes] = []
        self.activations: list[int] = []

    def renew(self, csr: bytes) -> IssuedCredential:
        self.renewed_csrs.append(csr)
        if self.fail_renew:
            self.fail_renew -= 1
            raise AgentTransportError("renew response lost")
        return self.issued

    def activate(self, generation: int, _credentials) -> None:
        self.activations.append(generation)
        if self.fail_activate:
            self.fail_activate -= 1
            raise AgentTransportError("activation response lost")

    def claim(self):
        if self.fail_claim:
            raise AgentTransportError("claim response lost")
        return super().claim()


def test_rotation_retries_same_csr_after_renew_response_loss_and_resumes_activation_after_restart(
    tmp_path: Path,
) -> None:
    ca_path, certificate_path, key_path, ca, ca_key = _credential_material(tmp_path)
    state_root = tmp_path / "state"
    store = CredentialStore(state_root, ca_path, certificate_path, key_path)
    pending = store.prepare_rotation(NODE_ID)
    issued = _issue_rotation(pending.csr_pem, ca, ca_key)
    first = RotationControl(issued, fail_renew=1)
    context = OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(state_root),
        probe=Probe(),
    )

    with pytest.raises(AgentTransportError):
        Agent(first, OperationRegistry(), context, credentials=store).run_once()
    assert store.pending_rotation() is not None
    first_csr = first.renewed_csrs[0]

    after_renew_restart = CredentialStore(
        state_root, ca_path, certificate_path, key_path
    )
    second = RotationControl(issued, fail_activate=1)
    failed_readiness: list[str] = []
    with pytest.raises(AgentTransportError):
        Agent(
            second,
            OperationRegistry(),
            context,
            credentials=after_renew_restart,
            on_authenticated_exchange=lambda: failed_readiness.append("ready"),
        ).run_once()
    assert second.renewed_csrs == [first_csr]
    assert after_renew_restart.active_generation == 1
    assert after_renew_restart.staged_generation == 2
    assert failed_readiness == []

    after_activation_restart = CredentialStore(
        state_root, ca_path, certificate_path, key_path
    )
    third = RotationControl(issued, fail_claim=True)
    activated_readiness: list[str] = []
    with pytest.raises(AgentTransportError):
        Agent(
            third,
            OperationRegistry(),
            context,
            credentials=after_activation_restart,
            on_authenticated_exchange=lambda: activated_readiness.append("ready"),
        ).run_once()

    assert third.renewed_csrs == []
    assert third.activations == [2]
    assert after_activation_restart.active_generation == 2
    assert after_activation_restart.staged_generation is None
    assert activated_readiness == ["ready"]


def test_installed_console_entry_point_has_bounded_help_without_loading_credentials(
    tmp_path: Path,
) -> None:
    project = Path(__file__).parents[1]
    result = subprocess.run(
        ["uv", "run", "--project", str(project), "dgx-forge-agent", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "outbound Spark agent" in result.stdout
