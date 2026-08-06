from __future__ import annotations

import json
import os
import socket
import threading
from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from dgx_agent.package_helper import (
    Ed25519ReceiptVerifier,
    PackageHelper,
    SignedFenceAuthorizer,
    SystemdBackendLauncher,
    main,
    serve_connection,
)
from dgx_agent.package_helper_protocol import (
    HelperProtocolError,
    HelperRequest,
    HelperResponse,
    SignedObjectReceipt,
    canonical_helper_document,
    frame_helper_message,
    receive_helper_message,
)
from dgx_agent.packages.backends import BackendInvocation
from dgx_agent.packages.sandbox import SandboxPolicy
from packages.test_backends import OBJECT, invocation_document

REQUEST_ID = "11111111-1111-4111-8111-111111111111"
JOB_ID = "22222222-2222-4222-8222-222222222222"
OPERATION_ID = "33333333-3333-4333-8333-333333333333"
FENCE = "44444444-4444-4444-8444-444444444444"
SIGNATURE = "A" * 86


def request_document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "request_id": REQUEST_ID,
        "job_id": JOB_ID,
        "operation_id": OPERATION_ID,
        "attempt": 1,
        "fence": FENCE,
        "invocation": invocation_document(),
        "receipts": [
            {
                "schema_version": 1,
                "object_digest": OBJECT,
                "size": 4096,
                "relative_name": f"objects/sha256/{OBJECT}",
                "signature": SIGNATURE,
            }
        ],
        "authorization": SIGNATURE,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }


def test_helper_protocol_requires_duplicate_free_canonical_json() -> None:
    raw = canonical_helper_document(request_document())
    request = HelperRequest.parse(raw)

    assert request.invocation == BackendInvocation.parse(invocation_document())
    assert request.receipts == (
        SignedObjectReceipt(1, OBJECT, 4096, f"objects/sha256/{OBJECT}", SIGNATURE),
    )

    reordered = json.dumps(request_document(), separators=(",", ":")).encode()
    duplicate = raw.replace(b'{"attempt":1,', b'{"attempt":1,"attempt":1,', 1)
    with pytest.raises(HelperProtocolError, match="canonical"):
        HelperRequest.parse(reordered)
    with pytest.raises(HelperProtocolError, match="duplicate"):
        HelperRequest.parse(duplicate)


class ReceiptVerifier:
    def __init__(self) -> None:
        self.checked: list[SignedObjectReceipt] = []

    def verify(self, receipt: SignedObjectReceipt) -> bool:
        self.checked.append(receipt)
        return True


class FenceAuthorizer:
    def __init__(self, permitted: bool = True) -> None:
        self.permitted = permitted

    def authorize(self, request: HelperRequest, request_digest: str) -> bool:
        return self.permitted and len(request_digest) == 64


class Launcher:
    def __init__(self) -> None:
        self.plans = []

    def launch(self, request: HelperRequest, sandbox: SandboxPolicy):
        self.plans.append((request, sandbox))
        return {
            "status": "launched",
            "evidence_digest": "c" * 64,
            "fence": request.fence,
        }


def _helper(*, permitted: bool = True, agent_uid: int = 64000):
    verifier = ReceiptVerifier()
    launcher = Launcher()
    helper = PackageHelper(
        agent_uid=agent_uid,
        sandbox=SandboxPolicy(
            workload_uid=64001,
            workload_gid=64001,
            allowed_devices=("nvidia0",),
        ),
        receipt_verifier=verifier,
        fence_authorizer=FenceAuthorizer(permitted),
        launcher=launcher,
    )
    return helper, verifier, launcher


def test_helper_rejects_non_agent_peer_before_parsing_or_launching() -> None:
    helper, verifier, launcher = _helper()

    with pytest.raises(HelperProtocolError, match="peer"):
        helper.handle(64002, b"not-json")
    assert verifier.checked == []
    assert launcher.plans == []


def test_helper_verifies_receipts_and_fence_then_returns_bound_evidence() -> None:
    helper, verifier, launcher = _helper()

    raw = helper.handle(64000, canonical_helper_document(request_document()))
    response = HelperResponse.parse(raw)

    assert response.status == "launched"
    assert response.fence == FENCE
    assert response.evidence_digest == "c" * 64
    assert len(verifier.checked) == 1
    assert launcher.plans[0][1].workload_uid == 64001


def test_helper_rejects_stale_fence_and_request_replay() -> None:
    stale, _, stale_launcher = _helper(permitted=False)
    raw = canonical_helper_document(request_document())

    with pytest.raises(HelperProtocolError, match="fence"):
        stale.handle(64000, raw)
    assert stale_launcher.plans == []

    helper, _, launcher = _helper()
    helper.handle(64000, raw)
    with pytest.raises(HelperProtocolError, match="replay"):
        helper.handle(64000, raw)
    assert len(launcher.plans) == 1


def test_helper_rejects_unsigned_receipt_and_cross_fence_launcher_result() -> None:
    class RejectingVerifier:
        def verify(self, _receipt):
            return False

    launcher = Launcher()
    helper = PackageHelper(
        agent_uid=64000,
        sandbox=SandboxPolicy(64001, 64001, allowed_devices=("nvidia0",)),
        receipt_verifier=RejectingVerifier(),
        fence_authorizer=FenceAuthorizer(),
        launcher=launcher,
    )
    with pytest.raises(HelperProtocolError, match="receipt"):
        helper.handle(64000, canonical_helper_document(request_document()))

    class WrongFenceLauncher(Launcher):
        def launch(self, request, sandbox):
            return {
                "status": "launched",
                "evidence_digest": "c" * 64,
                "fence": "55555555-5555-4555-8555-555555555555",
            }

    helper = PackageHelper(
        agent_uid=64000,
        sandbox=SandboxPolicy(64001, 64001, allowed_devices=("nvidia0",)),
        receipt_verifier=ReceiptVerifier(),
        fence_authorizer=FenceAuthorizer(),
        launcher=WrongFenceLauncher(),
    )
    with pytest.raises(HelperProtocolError, match="fence"):
        helper.handle(64000, canonical_helper_document(request_document()))


def test_signed_fence_authorizer_rejects_invalid_and_replayed_grants_after_restart(
    tmp_path: Path,
) -> None:
    private = Ed25519PrivateKey.generate()
    public_path = tmp_path / "fence-public.pem"
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    public_path.chmod(0o644)
    replay = tmp_path / "helper-replay.sqlite3"
    document = request_document()
    document["invocation"]["network"] = {"mode": "none", "egress": []}
    unsigned = dict(document)
    del unsigned["authorization"]
    document["authorization"] = (
        urlsafe_b64encode(private.sign(canonical_helper_document(unsigned)))
        .decode()
        .rstrip("=")
    )
    request = HelperRequest.parse(canonical_helper_document(document))

    first = SignedFenceAuthorizer.from_file(
        public_path, replay, allow_unprivileged_test_files=True
    )
    assert first.authorize(request, request.digest) is True
    restarted = SignedFenceAuthorizer.from_file(
        public_path, replay, allow_unprivileged_test_files=True
    )
    assert restarted.authorize(request, request.digest) is False

    changed = request_document()
    changed["attempt"] = 2
    stale = HelperRequest.parse(canonical_helper_document(changed))
    assert restarted.authorize(stale, stale.digest) is False


def test_signed_fence_authorizer_rejects_expired_grants_and_caps_replay_state(
    tmp_path: Path,
) -> None:
    private = Ed25519PrivateKey.generate()
    public_path = tmp_path / "fence-public.pem"
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    public_path.chmod(0o644)
    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)

    def signed_document(request_id: str, fence: str, expires_at: datetime):
        document = request_document()
        document["request_id"] = request_id
        document["fence"] = fence
        document["expires_at"] = expires_at.isoformat().replace("+00:00", "Z")
        unsigned = dict(document)
        del unsigned["authorization"]
        document["authorization"] = (
            urlsafe_b64encode(private.sign(canonical_helper_document(unsigned)))
            .decode()
            .rstrip("=")
        )
        return HelperRequest.parse(canonical_helper_document(document))

    authorizer = SignedFenceAuthorizer.from_file(
        public_path,
        tmp_path / "bounded-replay.sqlite3",
        allow_unprivileged_test_files=True,
        clock=lambda: now,
        max_entries=1,
    )
    expired = signed_document(REQUEST_ID, FENCE, now - timedelta(seconds=1))
    assert authorizer.authorize(expired, expired.digest) is False
    first = signed_document(REQUEST_ID, FENCE, now + timedelta(minutes=5))
    assert authorizer.authorize(first, first.digest) is True
    second = signed_document(
        "55555555-5555-4555-8555-555555555555",
        "66666666-6666-4666-8666-666666666666",
        now + timedelta(minutes=5),
    )
    with pytest.raises(HelperProtocolError, match="capacity"):
        authorizer.authorize(second, second.digest)


def test_ed25519_receipt_verifier_accepts_only_exact_canonical_receipt_signature(
    tmp_path: Path,
) -> None:
    private = Ed25519PrivateKey.generate()
    public_path = tmp_path / "receipt-public.pem"
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    public_path.chmod(0o644)
    unsigned = SignedObjectReceipt(
        1, OBJECT, 4096, f"objects/sha256/{OBJECT}", SIGNATURE
    )
    signature = (
        urlsafe_b64encode(
            private.sign(canonical_helper_document(unsigned.unsigned_mapping()))
        )
        .decode()
        .rstrip("=")
    )
    receipt = SignedObjectReceipt(
        1, OBJECT, 4096, f"objects/sha256/{OBJECT}", signature
    )
    verifier = Ed25519ReceiptVerifier.from_file(
        public_path, allow_unprivileged_test_file=True
    )

    assert verifier.verify(receipt) is True
    assert (
        verifier.verify(
            SignedObjectReceipt(1, OBJECT, 4097, f"objects/sha256/{OBJECT}", signature)
        )
        is False
    )


def test_socket_helper_uses_length_frame_without_waiting_for_peer_eof() -> None:
    agent_uid = os.geteuid()
    if agent_uid == 0:
        pytest.skip("socket peer identity test requires an unprivileged test UID")
    helper, _, _ = _helper(agent_uid=agent_uid)
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    failure: list[BaseException] = []

    def run() -> None:
        try:
            serve_connection(helper, server, timeout_seconds=1.0)
        except (HelperProtocolError, OSError) as error:
            failure.append(error)
        finally:
            server.close()

    thread = threading.Thread(target=run)
    thread.start()
    try:
        client.sendall(
            frame_helper_message(canonical_helper_document(request_document()))
        )
        response = HelperResponse.parse(
            receive_helper_message(client, timeout_seconds=1.0)
        )
        assert response.fence == FENCE
    finally:
        client.close()
        thread.join(2)
    assert failure == []


def test_concrete_launcher_uses_sealed_content_and_fixed_systemd_sandbox(
    tmp_path: Path,
) -> None:
    generations = tmp_path / "generations"
    executable = generations / ("a" * 64) / "gen-20260806-a" / "bin" / "future-adapter"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"signed future adapter")
    executable.chmod(0o500)
    digest = __import__("hashlib").sha256(executable.read_bytes()).hexdigest()
    objects = tmp_path / "objects" / "sha256"
    objects.mkdir(parents=True)
    mount_content = b"signed model content"
    mount_digest = __import__("hashlib").sha256(mount_content).hexdigest()
    mount_object = objects / mount_digest
    mount_object.write_bytes(mount_content)
    mount_object.chmod(0o400)
    document = request_document()
    document["invocation"]["network"] = {"mode": "none", "egress": []}
    document["invocation"]["mounts"] = [
        {
            "object_digest": mount_digest,
            "target": "models/primary",
            "read_only": True,
        }
    ]
    document["receipts"] = [
        {
            "schema_version": 1,
            "object_digest": digest,
            "size": executable.stat().st_size,
            "relative_name": f"objects/sha256/{digest}",
            "signature": SIGNATURE,
        },
        {
            "schema_version": 1,
            "object_digest": mount_digest,
            "size": len(mount_content),
            "relative_name": f"objects/sha256/{mount_digest}",
            "signature": SIGNATURE,
        },
    ]
    request = HelperRequest.parse(canonical_helper_document(document))

    class Runner:
        def __init__(self):
            self.calls = []
            self.cleaned = []

        def run(self, argv, *, pass_fds, timeout_seconds):
            metadata = os.fstat(pass_fds[1])
            assert metadata.st_uid == os.geteuid()
            assert metadata.st_gid == os.getegid()
            assert metadata.st_mode & 0o777 == 0o500
            self.calls.append((argv, pass_fds, timeout_seconds))
            return 0

        def cleanup(self, unit_name):
            self.cleaned.append(unit_name)

    runner = Runner()
    launcher = SystemdBackendLauncher(generations, objects_root=objects, runner=runner)
    if os.geteuid() == 0:
        pytest.skip("snapshot ownership test requires an unprivileged test UID")
    sandbox = SandboxPolicy(os.geteuid(), os.getegid(), allowed_devices=("nvidia0",))

    result = launcher.launch(request, sandbox)

    assert result["status"] == "launched"
    assert result["fence"] == FENCE
    argv, pass_fds, timeout = runner.calls[0]
    assert argv[0] == "/usr/bin/systemd-run"
    assert f"--uid={os.geteuid()}" in argv
    assert f"--gid={os.getegid()}" in argv
    assert "--property=NoNewPrivileges=yes" in argv
    assert "--property=CapabilityBoundingSet=" in argv
    assert "--property=AmbientCapabilities=" in argv
    assert "--property=DevicePolicy=closed" in argv
    assert argv.count("--property=PrivateNetwork=yes") == 1
    assert "--property=RuntimeMaxSec=60" in argv
    assert len(pass_fds) == 3
    mount_source = f"/proc/{os.getpid()}/fd/{pass_fds[2]}"
    assert (
        f"--property=BindReadOnlyPaths={mount_source}:"
        "/run/dgx-forge/generation/models/primary"
    ) in argv
    assert timeout == 60
    assert runner.cleaned == []


def test_launcher_rejects_restricted_network_before_content_or_side_effects(
    tmp_path: Path,
) -> None:
    class Runner:
        def __init__(self):
            self.calls = []

        def run(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return 0

        def cleanup(self, unit_name):
            self.calls.append(("cleanup", unit_name))

    runner = Runner()
    request = HelperRequest.parse(canonical_helper_document(request_document()))
    launcher = SystemdBackendLauncher(tmp_path / "missing", runner=runner)

    with pytest.raises(HelperProtocolError, match="network-policy boundary"):
        launcher.launch(
            request,
            SandboxPolicy(64001, 64001, allowed_devices=("nvidia0",)),
        )
    assert runner.calls == []


def test_launcher_cleans_failed_transient_unit(tmp_path: Path) -> None:
    generations = tmp_path / "generations"
    executable = generations / ("a" * 64) / "gen-20260806-a" / "bin" / "future-adapter"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"signed future adapter")
    executable.chmod(0o500)
    digest = __import__("hashlib").sha256(executable.read_bytes()).hexdigest()
    document = request_document()
    document["invocation"]["network"] = {"mode": "none", "egress": []}
    document["invocation"]["mounts"] = []
    document["receipts"] = [
        {
            "schema_version": 1,
            "object_digest": digest,
            "size": executable.stat().st_size,
            "relative_name": f"objects/sha256/{digest}",
            "signature": SIGNATURE,
        }
    ]
    request = HelperRequest.parse(canonical_helper_document(document))

    class FailingRunner:
        def __init__(self):
            self.cleaned = []

        def run(self, argv, *, pass_fds, timeout_seconds):
            raise HelperProtocolError("package backend launch timed out")

        def cleanup(self, unit_name):
            self.cleaned.append(unit_name)

    runner = FailingRunner()
    if os.geteuid() == 0:
        pytest.skip("snapshot ownership test requires an unprivileged test UID")
    launcher = SystemdBackendLauncher(generations, runner=runner)

    with pytest.raises(HelperProtocolError, match="timed out"):
        launcher.launch(
            request,
            SandboxPolicy(os.geteuid(), os.getegid(), allowed_devices=("nvidia0",)),
        )
    assert runner.cleaned == [f"dgx-workload-{REQUEST_ID}.service"]


def test_helper_cli_requires_exact_systemd_socket_activation(monkeypatch) -> None:
    monkeypatch.delenv("LISTEN_PID", raising=False)
    monkeypatch.delenv("LISTEN_FDS", raising=False)

    with pytest.raises(HelperProtocolError, match="systemd socket activation"):
        main(["--listen-fd=3"])
