from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from cluster_profiles.fleet import ManagementEndpoint, NodeId
from cluster_profiles.fleet.install_contracts import InstallationRequest
from cluster_profiles.install.orchestrator import WaitForOperator
from cluster_profiles.install.remote import RemoteResult
from cluster_profiles.install.steps import (
    ProductionStepOptions,
    _json_result,
    build_production_handlers,
)

ROOT = Path(__file__).resolve().parents[3]
SERIAL_DIGEST = "a" * 64
MACHINE_DIGEST = "b" * 64
HOST_FINGERPRINT = "SHA256:host-key"
ADMIN_FINGERPRINT = "SHA256:admin-key"


def test_remote_nonobject_json_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="non-object"):
        _json_result(RemoteResult(0, b"[]", b""), "fixture")


class ScriptedTransport:
    def __init__(self) -> None:
        self.runs: list[tuple[ManagementEndpoint, tuple[str, ...], bytes]] = []
        self.copies: list[tuple[ManagementEndpoint, Path, str, int]] = []
        self.inventory_count = 0

    def run(self, endpoint, argv, stdin, timeout):
        self.runs.append((endpoint, argv, stdin))
        if b"product_serial_sha256" in stdin:
            return RemoteResult(
                0,
                json.dumps(
                    {
                        "schema_version": 1,
                        "product_serial_sha256": SERIAL_DIGEST,
                        "machine_id_sha256": MACHINE_DIGEST,
                        "host_key_fingerprints": [HOST_FINGERPRINT],
                        "requires_console_repair": False,
                    }
                ).encode(),
                b"",
            )
        if b'"hostname"' in stdin or b"--arg hostname" in stdin:
            self.inventory_count += 1
            return RemoteResult(
                0,
                json.dumps(
                    {
                        "hostname": "dynamic-host",
                        "boot_id": f"boot-{self.inventory_count}",
                        "nvidia": [{"name": "GB10"}],
                        "docker": {"engine": {"Version": "1"}},
                        "interfaces": [],
                        "rdma": None,
                    }
                ).encode(),
                b"",
            )
        if "--check" in argv and "install-ssh-hardening" in " ".join(argv):
            return RemoteResult(2, b'{"status":"change-required"}\n', b"")
        return RemoteResult(0, b'{"status":"changed"}\n', b"")

    def copy(self, endpoint, source, destination, mode):
        self.copies.append((endpoint, source, destination, mode))
        return RemoteResult(0, b"", b"")


def _request() -> InstallationRequest:
    return InstallationRequest(
        node_id=NodeId.parse("spk_00000000000000000000000000000001"),
        display_name="alpha",
        endpoint=ManagementEndpoint(
            host="dynamic.local",
            user="operator",
            credential_ref="secret://ssh/admin",
        ),
        labels={},
    )


def _options(tmp_path: Path) -> ProductionStepOptions:
    public_key = tmp_path / "admin.pub"
    public_key.write_text("ssh-ed25519 AAAATEST admin\n")
    return ProductionStepOptions(
        repository_root=ROOT,
        admin_public_key=public_key,
        admin_key_fingerprint=ADMIN_FINGERPRINT,
        trusted_serial_sha256=SERIAL_DIGEST,
        trusted_host_key_fingerprints=(HOST_FINGERPRINT,),
        recovery_verified=True,
    )


def test_production_handlers_execute_every_gate_for_only_requested_endpoint(
    tmp_path: Path,
) -> None:
    transport = ScriptedTransport()
    handlers = build_production_handlers(_options(tmp_path), transport)
    request = _request()

    results = [handlers[name](request) for name in (
        "identity",
        "pre-inventory",
        "public-key",
        "ssh-hardening",
        "node-policy",
        "post-inventory",
        "acceptance",
    )]

    assert all(result.stdout or result.stderr == b"" for result in results)
    assert all(call[0] == request.endpoint for call in transport.runs)
    assert all(copy[0] == request.endpoint for copy in transport.copies)
    assert all("node1" not in " ".join(call[1]) for call in transport.runs)
    assert all("192.168." not in " ".join(call[1]) for call in transport.runs)
    assert {copy[2] for copy in transport.copies} >= {
        "/tmp/vonk-install-ssh-hardening",
        "/tmp/vonk-install-ssh-drop-in.conf",
        "/tmp/vonk-apply-node-policy",
        "/tmp/vonk-disable-earlyoom",
        "/tmp/vonk-node-policy.json",
    }


def test_identity_without_trusted_console_assertion_waits(tmp_path: Path) -> None:
    transport = ScriptedTransport()
    options = _options(tmp_path)
    options = ProductionStepOptions(
        repository_root=options.repository_root,
        admin_public_key=options.admin_public_key,
        admin_key_fingerprint=options.admin_key_fingerprint,
        trusted_serial_sha256=None,
        trusted_host_key_fingerprints=(),
        recovery_verified=options.recovery_verified,
    )

    with pytest.raises(WaitForOperator, match="trusted assertion"):
        build_production_handlers(options, transport)["identity"](_request())


def test_public_key_and_hardening_wait_for_required_operator_inputs(
    tmp_path: Path,
) -> None:
    transport = ScriptedTransport()
    options = ProductionStepOptions(
        repository_root=ROOT,
        admin_public_key=None,
        admin_key_fingerprint=None,
        trusted_serial_sha256=SERIAL_DIGEST,
        trusted_host_key_fingerprints=(HOST_FINGERPRINT,),
        recovery_verified=False,
    )
    handlers = build_production_handlers(options, transport)

    with pytest.raises(WaitForOperator, match="public key"):
        handlers["public-key"](_request())
    with pytest.raises(WaitForOperator, match="recovery"):
        handlers["ssh-hardening"](_request())


def test_tampered_repository_script_is_not_silently_accepted(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    (repository / "nodes" / "bin").mkdir(parents=True)
    (repository / "nodes" / "etc" / "ssh" / "sshd_config.d").mkdir(parents=True)
    (repository / "nodes" / "policy").mkdir(parents=True)
    required = {
        "inspect-node-identity": "identity",
        "collect-inventory": "inventory",
        "install-ssh-hardening": "hardening",
        "apply-node-policy": "policy",
        "disable-earlyoom": "earlyoom",
    }
    for name, content in required.items():
        (repository / "nodes" / "bin" / name).write_text(content)
    (repository / "nodes" / "etc" / "ssh" / "sshd_config.d" / "90-vonk-admin.conf").write_text("drop-in")
    (repository / "nodes" / "policy" / "default.json").write_text("{}")
    options = ProductionStepOptions(
        repository_root=repository,
        admin_public_key=None,
        admin_key_fingerprint=None,
        trusted_serial_sha256=None,
        trusted_host_key_fingerprints=(),
        recovery_verified=False,
        expected_artifact_sha256={"inspect-node-identity": hashlib.sha256(b"different").hexdigest()},
    )

    with pytest.raises(ValueError, match="digest"):
        build_production_handlers(options, ScriptedTransport())
