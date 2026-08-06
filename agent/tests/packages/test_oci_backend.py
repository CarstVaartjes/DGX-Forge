from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from dgx_agent.package_helper_protocol import HelperExecutionBody, HelperRequest
from dgx_agent.packages.backends import (
    Backend,
    BackendInvocation,
    NetworkPolicy,
    ResourcePolicy,
)
from dgx_agent.packages.oci_backend import (
    OciBackendError,
    OciBackendLauncher,
    OciRuntimeCapability,
)
from dgx_agent.packages.sandbox import SandboxPolicy
from dgx_agent_protocol import OciBundleMetadata
from dgx_agent_protocol.workload_packages import (
    PACKAGE_HELPER_AUTHORITY,
    PackageHelperGrantClaims,
    PackageHelperOperation,
    PackageHelperSignature,
    PackageObjectReceiptClaims,
    SignedPackageHelperGrant,
    SignedPackageObjectReceipt,
)

RELEASE = "a" * 64
GENERATION = "gen-future"
REQUEST = "11111111-1111-4111-8111-111111111111"
JOB = "22222222-2222-4222-8222-222222222222"
OPERATION = "33333333-3333-4333-8333-333333333333"
FENCE = "44444444-4444-4444-8444-444444444444"
NODE = "spk_" + "1" * 32


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _request(tmp_path: Path) -> tuple[HelperRequest, OciBundleMetadata]:
    executable = b"#!/bin/sh\necho future\n"
    rootfs_entries = [
        {"kind": "directory", "mode": 0o555, "path": "usr"},
        {"kind": "directory", "mode": 0o555, "path": "usr/bin"},
        {"digest": hashlib.sha256(executable).hexdigest(), "kind": "file", "mode": 0o555, "path": "usr/bin/server", "size": len(executable)},
    ]
    rootfs_digest = hashlib.sha256(_canonical(rootfs_entries)).hexdigest()
    config = {"architecture": "arm64", "root": {"readonly": True}}
    image_manifest = {"schemaVersion": 2, "config": {"digest": "sha256:" + hashlib.sha256(_canonical(config)).hexdigest()}, "layers": []}
    metadata = OciBundleMetadata(
        1,
        "runtime",
        "sha256:" + hashlib.sha256(_canonical(image_manifest)).hexdigest(),
        "sha256:" + hashlib.sha256(_canonical(config)).hexdigest(),
        "sha256:" + rootfs_digest,
        "linux-arm64",
        "runc",
        "rootfs",
        "usr/bin/server",
    )
    component = tmp_path / "generations" / RELEASE / GENERATION / "components" / "runtime"
    rootfs = component / "rootfs/usr/bin"
    rootfs.mkdir(parents=True)
    (rootfs / "server").write_bytes(executable)
    (rootfs / "server").chmod(0o555)
    (component / "oci-bundle.json").write_bytes(_canonical(metadata.to_mapping()))
    (component / "oci-manifest.json").write_bytes(_canonical(image_manifest))
    (component / "config.json").write_bytes(_canonical(config))
    for path in (component, component / "rootfs", component / "rootfs/usr", rootfs):
        path.chmod(0o555)
    bundle_digest = hashlib.sha256((component / "oci-bundle.json").read_bytes()).hexdigest()
    invocation = BackendInvocation(
        1, Backend.OCI, RELEASE, GENERATION, "usr/bin/server", ("health",),
        ResourcePolicy(1000, 1024**3, 64, 60, 65536), (), (), NetworkPolicy("none"), None, metadata, bundle_digest
    )
    digests = []
    for path in (component / "oci-bundle.json", component / "config.json"):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        claims = PackageObjectReceiptClaims(1, PACKAGE_HELPER_AUTHORITY, digest, path.stat().st_size, "objects/sha256/" + digest)
        digests.append(SignedPackageObjectReceipt(claims, PackageHelperSignature("ed25519", "b" * 64, "a" * 128)))
    claims = PackageObjectReceiptClaims(
        1, PACKAGE_HELPER_AUTHORITY, bundle_digest,
        (component / "oci-bundle.json").stat().st_size,
        "objects/sha256/" + bundle_digest,
    )
    digests.append(
        SignedPackageObjectReceipt(
            claims, PackageHelperSignature("ed25519", "b" * 64, "a" * 128)
        )
    )
    body = HelperExecutionBody(1, REQUEST, NODE, JOB, OPERATION, 1, FENCE, PackageHelperOperation.HEALTH, invocation, tuple(digests))
    issued = 1_900_000_000
    claims = PackageHelperGrantClaims(1, PACKAGE_HELPER_AUTHORITY, REQUEST, NODE, JOB, OPERATION, 1, FENCE, RELEASE, GENERATION, PackageHelperOperation.HEALTH, body.digest, issued, issued + 60)
    grant = SignedPackageHelperGrant(claims, PackageHelperSignature("ed25519", "c" * 64, "d" * 128))
    return HelperRequest(body, grant), metadata


def test_runtime_capability_rejects_non_fixed_executable(tmp_path: Path) -> None:
    with pytest.raises(OciBackendError, match="fixed"):
        OciRuntimeCapability("a" * 64, executable=tmp_path / "runc")


def test_oci_launcher_requires_signed_metadata_and_fixed_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if __import__("os").geteuid() != 0:
        pytest.skip("runtime capability ownership check requires root")
    import dgx_agent.packages.oci_backend as module

    runtime = tmp_path / "runc"
    runtime.write_bytes(b"runc-test")
    runtime.chmod(0o755)
    monkeypatch.setattr(module, "_RUNC", runtime)
    capability = OciRuntimeCapability(hashlib.sha256(runtime.read_bytes()).hexdigest(), executable=runtime)
    request, _ = _request(tmp_path)

    class Runner:
        def __init__(self):
            self.calls = []

        def run(self, argv, *, timeout_seconds):
            self.calls.append((argv, timeout_seconds))
            return 0

        def cleanup(self, container_id):
            self.calls.append(("cleanup", container_id))

    runner = Runner()
    result = OciBackendLauncher(tmp_path / "generations", tmp_path / "oci-state", capability=capability, runner=runner).launch(
        request, SandboxPolicy(65534, 65534)
    )
    assert result["status"] == "launched"
    assert runner.calls[0][0][0] == str(runtime)
    assert "docker.sock" not in " ".join(runner.calls[0][0])
