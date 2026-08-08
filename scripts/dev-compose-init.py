#!/usr/bin/env python3
"""Initialize the synthetic active generation used by local dev Compose."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from vonk_control.host_state import HostOperationPlan, SelectionReceipt

_TARGET_SHA256 = "0" * 64
_BUILD_DIGEST = "sha256:" + "1" * 64
_VERSION = "0.1.0"
_DATABASE_REVISION = "0020_recipe_catalog_bridge"
_API_IMAGE = "vonk-forge-dev/control-api@sha256:" + "0" * 64
_WORKER_IMAGE = "vonk-forge-dev/control-worker@sha256:" + "1" * 64


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write_atomic(path: Path, raw: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.new")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o644,
    )
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _stage_runtime_secrets() -> None:
    source_root = os.environ.get("VONK_DEV_SECRET_SOURCE_ROOT")
    runtime_root = os.environ.get("VONK_DEV_RUNTIME_SECRET_ROOT")
    if not source_root or not runtime_root:
        return
    source = Path(source_root)
    destination = Path(runtime_root)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    admin_key = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    values = {
        "database-url": (source / "database-url").read_bytes(),
        "git-signing-key": (source / "git-signing-key").read_bytes(),
        "worker-api-token": secrets.token_urlsafe(32).encode("ascii"),
        "admin-grant-private-key": admin_key,
    }
    for name, raw in values.items():
        target = destination / name
        _write_atomic(target, raw)
        os.chown(target, 10001, 10001)
        os.chmod(target, 0o400 if name == "admin-grant-private-key" else 0o440)
    os.chown(destination, 10001, 10001)
    os.chmod(destination, 0o550)


def _active_projection() -> bytes:
    target_name = f"platform/releases/{_VERSION}/{_TARGET_SHA256}.json"
    plan = HostOperationPlan(
        operation_id="dev-compose",
        plan_digest="sha256:" + "2" * 64,
        generation_id="gen-" + _TARGET_SHA256[:24],
        platform_target_name=target_name,
        platform_target_sha256=_TARGET_SHA256,
        tuf_targets_version=1,
        release_digest="sha256:" + _TARGET_SHA256,
        build_digest=_BUILD_DIGEST,
        platform_version=_VERSION,
        deployment_bundle_digest="sha256:" + "3" * 64,
        api_image=_API_IMAGE,
        worker_image=_WORKER_IMAGE,
        database_revision=_DATABASE_REVISION,
    )
    selection = SelectionReceipt.from_plan(plan, previous_generation=None)
    generation_raw = _canonical(selection.generation.document())
    selection_raw = _canonical(selection.document())
    document = {
        "generation_receipt_sha256": hashlib.sha256(generation_raw).hexdigest(),
        "projection_kind": "active",
        "projection_sequence": 1,
        "schema_version": 1,
        "selection": selection.document(),
        "selection_receipt_sha256": hashlib.sha256(selection_raw).hexdigest(),
    }
    return _canonical(document)


def main() -> int:
    identity_root = Path(os.environ.get("VONK_CONTROL_IDENTITY_ROOT", "/control-identity"))
    identity_root.mkdir(mode=0o755, parents=True, exist_ok=True)
    os.chown(identity_root, 0, 0)
    os.chmod(identity_root, 0o755)
    for directory in (Path("/state"), Path("/routes"), Path("/supervisor")):
        directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        os.chown(directory, 10001, 10001)
        os.chmod(directory, 0o750)
    _stage_runtime_secrets()
    active = identity_root / "active.json"
    _write_atomic(active, _active_projection())
    os.chown(active, 0, 0)
    os.chmod(active, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
