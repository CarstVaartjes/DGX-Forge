from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import fcntl
import json
import os
from pathlib import Path
import threading
import time
import ssl
import socket
import stat
from urllib.parse import urlsplit

import pytest
from securesystemslib.signer import CryptoSigner
from tuf.api.exceptions import DownloadError, DownloadHTTPError
from tuf.api.metadata import (
    DelegatedRole,
    Delegations,
    MetaFile,
    Metadata,
    Root,
    Snapshot,
    TargetFile,
    Targets,
    Timestamp,
)
from tuf.ngclient import FetcherInterface

from dgx_agent.releases import (
    ReleaseDescriptor,
    ReleaseDisposition,
    ReleaseEvidence,
    ReleaseInspection,
    ReleaseRequest,
    ReleaseInstallError,
    ReleaseInstaller,
    ReleaseValidationError,
    verify_installed_release,
    verify_release_tree,
)
from dgx_agent.update_trust import BoundedHTTPSFetcher, TUFReleaseTrust, TUFTrustError
import dgx_agent.update_trust as update_trust
import dgx_agent.releases as release_module
import dgx_agent.oci as oci_module
from dgx_agent.deadlines import DeadlineBindingError, MonotonicDeadline
from dgx_agent.oci import OCIError, ORASClient, ORASPolicy
from dgx_agent.probe import ProcessOutcome


VALID_RELEASE = {
    "schema_version": 1,
    "target_name": "spark-runtime-2026-08",
    "oci_manifest_digest": "sha256:" + "1" * 64,
    "target_digest": "2" * 64,
    "provenance_digest": "3" * 64,
    "adapter_id": "spark-runtime-v1",
}


def _descriptor() -> dict[str, object]:
    return {
        "schema_version": 1,
        "target_name": VALID_RELEASE["target_name"],
        "target_digest": VALID_RELEASE["target_digest"],
        "target_length": 17,
        "registry_origin": "https://registry.test.example",
        "repository": "dgx/releases",
        "oci_manifest_digest": VALID_RELEASE["oci_manifest_digest"],
        "provenance_digest": VALID_RELEASE["provenance_digest"],
        "adapter_id": VALID_RELEASE["adapter_id"],
        "adapter_version": "1.0.0",
        "architecture": "linux-arm64",
        "agent_min_version": "0.1.0",
        "agent_max_version": "0.1.0",
        "protocol_min_version": 1,
        "protocol_max_version": 1,
        "members": [
            {
                "path": "bin/runtime-adapter",
                "sha256": hashlib.sha256(b"x" * 17).hexdigest(),
                "size": 17,
                "mode": 0o500,
                "uid": __import__("os").geteuid(),
                "gid": __import__("os").getegid(),
            }
        ],
    }


class RepositoryFetcher(FetcherInterface):
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.urls: list[str] = []
        self.deadline = float("inf")

    def set_deadline(self, absolute_monotonic: float) -> None:
        self.deadline = absolute_monotonic

    def _fetch(self, url: str):
        self.urls.append(url)
        name = urlsplit(url).path.rsplit("/", 1)[-1]
        try:
            yield self.files[name]
        except KeyError as error:
            raise DownloadHTTPError("missing", 404) from error


def _signed_repository(
    descriptor: dict[str, object], *, expired: bool = False,
    bad_threshold: bool = False, version: int = 1,
    signers: dict[str, CryptoSigner] | None = None,
    root_bytes: bytes | None = None,
    target_length_override: int | None = None,
) -> tuple[bytes, RepositoryFetcher]:
    expiry = datetime.now(UTC) + (-timedelta(days=1) if expired else timedelta(days=1))
    signers = signers or {
        role: CryptoSigner.generate_ed25519()
        for role in ("root", "timestamp", "snapshot", "targets")
    }
    if root_bytes is None:
        root = Root(expires=expiry, consistent_snapshot=False)
        for role, signer in signers.items():
            root.add_key(signer.public_key, role)
        if bad_threshold:
            second_targets = CryptoSigner.generate_ed25519()
            root.add_key(second_targets.public_key, "targets")
            root.roles["targets"].threshold = 2
        root_metadata = Metadata(root)
        root_metadata.sign(signers["root"])
        root_bytes = root_metadata.to_bytes()

    target_bytes = (json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n").encode()
    target = (
        TargetFile(
            target_length_override,
            {"sha256": hashlib.sha256(target_bytes).hexdigest()},
            str(descriptor["target_name"]),
        )
        if target_length_override is not None
        else TargetFile.from_data(
            str(descriptor["target_name"]), target_bytes, ["sha256"]
        )
    )
    target.unrecognized_fields["custom"] = {"release": descriptor}
    targets_metadata = Metadata(
        Targets(version=version, expires=expiry, targets={str(descriptor["target_name"]): target})
    )
    targets_metadata.sign(signers["targets"])
    targets_bytes = targets_metadata.to_bytes()

    snapshot_metadata = Metadata(
        Snapshot(
            version=version,
            expires=expiry,
            meta={"targets.json": MetaFile.from_data(version, targets_bytes, ["sha256"])},
        )
    )
    snapshot_metadata.sign(signers["snapshot"])
    snapshot_bytes = snapshot_metadata.to_bytes()
    timestamp_metadata = Metadata(
        Timestamp(
            version=version,
            expires=expiry,
            snapshot_meta=MetaFile.from_data(version, snapshot_bytes, ["sha256"]),
        )
    )
    timestamp_metadata.sign(signers["timestamp"])
    fetcher = RepositoryFetcher(
        {
            "timestamp.json": timestamp_metadata.to_bytes(),
            "snapshot.json": snapshot_bytes,
            "targets.json": targets_bytes,
            str(descriptor["target_name"]): target_bytes,
        }
    )
    fetcher.signers = signers
    return root_bytes, fetcher


def _rotated_repository(invalid: bool = False) -> tuple[bytes, RepositoryFetcher]:
    expiry = datetime.now(UTC) + timedelta(days=1)
    old_root = CryptoSigner.generate_ed25519()
    new_root = CryptoSigner.generate_ed25519()
    signers = {
        role: CryptoSigner.generate_ed25519()
        for role in ("timestamp", "snapshot", "targets")
    }
    root1 = Root(version=1, expires=expiry, consistent_snapshot=False)
    root1.add_key(old_root.public_key, "root")
    for role, signer in signers.items():
        root1.add_key(signer.public_key, role)
    root1_metadata = Metadata(root1)
    root1_metadata.sign(old_root)
    root1_bytes = root1_metadata.to_bytes()

    root2 = Root(version=2, expires=expiry, consistent_snapshot=False)
    root2.add_key(new_root.public_key, "root")
    for role, signer in signers.items():
        root2.add_key(signer.public_key, role)
    root2_metadata = Metadata(root2)
    if not invalid:
        root2_metadata.sign(old_root)
    root2_metadata.sign(new_root, append=True)

    _, fetcher = _signed_repository(
        _descriptor(), signers={"root": new_root, **signers}, root_bytes=root1_bytes
    )
    fetcher.files["2.root.json"] = root2_metadata.to_bytes()
    return root1_bytes, fetcher


def _delegated_repository(invalid: bool = False) -> tuple[bytes, RepositoryFetcher]:
    descriptor = _descriptor()
    root_bytes, fetcher = _signed_repository(descriptor)
    expiry = datetime.now(UTC) + timedelta(days=1)
    delegated_signer = CryptoSigner.generate_ed25519()
    target_bytes = (json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n").encode()
    target = TargetFile.from_data(str(descriptor["target_name"]), target_bytes, ["sha256"])
    target.unrecognized_fields["custom"] = {"release": descriptor}
    delegated = Metadata(
        Targets(expires=expiry, targets={str(descriptor["target_name"]): target})
    )
    delegated.sign(delegated_signer)
    delegated_bytes = delegated.to_bytes()
    role = DelegatedRole(
        "spark", [delegated_signer.public_key.keyid], 2 if invalid else 1,
        True, paths=[str(descriptor["target_name"])],
    )
    top = Metadata(
        Targets(
            expires=expiry,
            targets={},
            delegations=Delegations(
                {delegated_signer.public_key.keyid: delegated_signer.public_key},
                {"spark": role},
            ),
        )
    )
    top.sign(fetcher.signers["targets"])
    top_bytes = top.to_bytes()
    snapshot = Metadata(
        Snapshot(
            expires=expiry,
            meta={
                "targets.json": MetaFile.from_data(1, top_bytes, ["sha256"]),
                "spark.json": MetaFile.from_data(1, delegated_bytes, ["sha256"]),
            },
        )
    )
    snapshot.sign(fetcher.signers["snapshot"])
    snapshot_bytes = snapshot.to_bytes()
    timestamp = Metadata(
        Timestamp(
            expires=expiry,
            snapshot_meta=MetaFile.from_data(1, snapshot_bytes, ["sha256"]),
        )
    )
    timestamp.sign(fetcher.signers["timestamp"])
    fetcher.files.update({
        "timestamp.json": timestamp.to_bytes(),
        "snapshot.json": snapshot_bytes,
        "targets.json": top_bytes,
        "spark.json": delegated_bytes,
        str(descriptor["target_name"]): target_bytes,
    })
    return root_bytes, fetcher


def test_release_request_accepts_only_the_exact_versioned_digest_boundary() -> None:
    request = ReleaseRequest.parse(VALID_RELEASE)

    assert request.target_name == "spark-runtime-2026-08"
    assert request.oci_manifest_digest == "sha256:" + "1" * 64
    assert request.target_digest == "2" * 64
    assert request.provenance_digest == "3" * 64
    assert request.adapter_id == "spark-runtime-v1"

    for changed in (
        VALID_RELEASE | {"command": ["id"]},
        VALID_RELEASE | {"registry": "https://attacker.invalid"},
        VALID_RELEASE | {"target_name": "../release"},
        VALID_RELEASE | {"oci_manifest_digest": "latest"},
        VALID_RELEASE | {"target_digest": "A" * 64},
        {key: value for key, value in VALID_RELEASE.items() if key != "adapter_id"},
    ):
        with pytest.raises(ReleaseValidationError):
            ReleaseRequest.parse(changed)


def test_release_evidence_and_inspection_are_bounded_typed_values() -> None:
    evidence = ReleaseEvidence(
        status="installed",
        release_digest="2" * 64,
        manifest_digest="sha256:" + "1" * 64,
        adapter_id="spark-runtime-v1",
    )
    inspection = ReleaseInspection(ReleaseDisposition.COMPLETED, evidence)

    assert evidence.to_mapping() == {
        "status": "installed",
        "release_digest": "2" * 64,
        "manifest_digest": "sha256:" + "1" * 64,
        "adapter_id": "spark-runtime-v1",
    }
    assert inspection.evidence is evidence


def test_real_tuf_updater_authorizes_exact_signed_release_descriptor(tmp_path: Path) -> None:
    root_bytes, fetcher = _signed_repository(_descriptor())
    trust = TUFReleaseTrust(
        metadata_root=tmp_path / "metadata",
        target_root=tmp_path / "targets",
        metadata_base_url="https://control.test.example/agent/v1/tuf/metadata/",
        target_base_url="https://control.test.example/agent/v1/tuf/targets/",
        bootstrap_root=root_bytes,
        fetcher=fetcher,
        registry_origin="https://registry.test.example",
        repository="dgx/releases",
        architecture="linux-arm64",
    )

    authorized = trust.authorize(
        ReleaseRequest.parse(VALID_RELEASE), datetime.now(UTC) + timedelta(seconds=2)
    )

    assert isinstance(authorized, ReleaseDescriptor)
    assert authorized.target_digest == "2" * 64
    assert authorized.members[0].path == "bin/runtime-adapter"
    assert fetcher.urls == [
        "https://control.test.example/agent/v1/tuf/metadata/2.root.json",
        "https://control.test.example/agent/v1/tuf/metadata/timestamp.json",
        "https://control.test.example/agent/v1/tuf/metadata/snapshot.json",
        "https://control.test.example/agent/v1/tuf/metadata/targets.json",
        "https://control.test.example/agent/v1/tuf/targets/spark-runtime-2026-08",
    ]


def test_real_tuf_rejects_expired_metadata_and_wrong_target_bytes(tmp_path: Path) -> None:
    expired_root, expired_fetcher = _signed_repository(_descriptor(), expired=True)
    with pytest.raises(TUFTrustError):
        TUFReleaseTrust(
            tmp_path / "expired-metadata", tmp_path / "expired-targets",
            "https://control.test.example/agent/v1/tuf/metadata/",
            "https://control.test.example/agent/v1/tuf/targets/",
            expired_root, expired_fetcher,
            "https://registry.test.example", "dgx/releases", "linux-arm64",
        ).authorize(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )

    root_bytes, fetcher = _signed_repository(_descriptor())
    fetcher.files["spark-runtime-2026-08"] = b"tampered"
    with pytest.raises(TUFTrustError):
        TUFReleaseTrust(
            tmp_path / "wrong-metadata", tmp_path / "wrong-targets",
            "https://control.test.example/agent/v1/tuf/metadata/",
            "https://control.test.example/agent/v1/tuf/targets/",
            root_bytes, fetcher,
            "https://registry.test.example", "dgx/releases", "linux-arm64",
        ).authorize(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )


def test_real_tuf_rejects_bad_signature_threshold_and_unsafe_cache_root(tmp_path: Path) -> None:
    root_bytes, fetcher = _signed_repository(_descriptor(), bad_threshold=True)
    with pytest.raises(TUFTrustError):
        TUFReleaseTrust(
            tmp_path / "threshold-metadata", tmp_path / "threshold-targets",
            "https://control.test.example/agent/v1/tuf/metadata/",
            "https://control.test.example/agent/v1/tuf/targets/",
            root_bytes, fetcher,
            "https://registry.test.example", "dgx/releases", "linux-arm64",
        ).authorize(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )


def test_tuf_rejects_oversized_signed_target_before_target_download(tmp_path: Path) -> None:
    root_bytes, fetcher = _signed_repository(
        _descriptor(), target_length_override=1024 * 1024 + 1
    )
    with pytest.raises(TUFTrustError, match="bounds"):
        TUFReleaseTrust(
            tmp_path / "metadata", tmp_path / "targets",
            "https://control.test.example/agent/v1/tuf/metadata/",
            "https://control.test.example/agent/v1/tuf/targets/",
            root_bytes, fetcher,
            "https://registry.test.example", "dgx/releases", "linux-arm64",
        ).authorize(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )
    assert not any(url.endswith("spark-runtime-2026-08") for url in fetcher.urls)


def test_tuf_target_memfd_is_write_sealed_before_descriptor_parsing() -> None:
    descriptor = os.memfd_create(
        "target-seal-test", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
    )
    try:
        os.write(descriptor, b"signed target")
        update_trust._seal_target_fd(descriptor)
        seals = fcntl.fcntl(descriptor, fcntl.F_GET_SEALS)
        assert seals & fcntl.F_SEAL_WRITE
        assert seals & fcntl.F_SEAL_GROW
        assert seals & fcntl.F_SEAL_SHRINK
        with pytest.raises(OSError):
            os.write(descriptor, b"tamper")
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("attack", ["rollback", "freeze", "mix-and-match"])
def test_real_tuf_rejects_version_and_consistency_attacks(tmp_path: Path, attack: str) -> None:
    root_bytes, fetcher = _signed_repository(_descriptor(), version=2)
    trust = TUFReleaseTrust(
        tmp_path / attack / "metadata", tmp_path / attack / "targets",
        "https://control.test.example/agent/v1/tuf/metadata/",
        "https://control.test.example/agent/v1/tuf/targets/",
        root_bytes, fetcher,
        "https://registry.test.example", "dgx/releases", "linux-arm64",
    )
    request = ReleaseRequest.parse(VALID_RELEASE)
    trust.authorize(request, datetime.now(UTC) + timedelta(seconds=2))

    replacement_root, replacement = _signed_repository(
        _descriptor(),
        version=1 if attack == "rollback" else 3,
        expired=attack == "freeze",
        signers=fetcher.signers,
        root_bytes=root_bytes,
    )
    assert replacement_root == root_bytes
    if attack == "mix-and-match":
        replacement.files["snapshot.json"] = fetcher.files["snapshot.json"]
    fetcher.files.update(replacement.files)

    with pytest.raises(TUFTrustError):
        trust.authorize(request, datetime.now(UTC) + timedelta(seconds=2))


@pytest.mark.parametrize(
    ("repository_factory", "accepted"),
    [
        (lambda: _rotated_repository(False), True),
        (lambda: _rotated_repository(True), False),
        (lambda: _delegated_repository(False), True),
        (lambda: _delegated_repository(True), False),
    ],
)
def test_real_tuf_enforces_root_rotation_and_delegation_thresholds(
    tmp_path: Path, repository_factory, accepted: bool
) -> None:
    root_bytes, fetcher = repository_factory()
    trust = TUFReleaseTrust(
        tmp_path / ("accepted" if accepted else "rejected") / "metadata",
        tmp_path / ("accepted" if accepted else "rejected") / "targets",
        "https://control.test.example/agent/v1/tuf/metadata/",
        "https://control.test.example/agent/v1/tuf/targets/",
        root_bytes, fetcher,
        "https://registry.test.example", "dgx/releases", "linux-arm64",
    )
    if accepted:
        result = trust.authorize(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )
        assert result.target_digest == "2" * 64
    else:
        with pytest.raises(TUFTrustError):
            trust.authorize(
                ReleaseRequest.parse(VALID_RELEASE),
                datetime.now(UTC) + timedelta(seconds=2),
            )



def test_tuf_rejects_symlinked_metadata_and_nonempty_target_cache(tmp_path: Path) -> None:
    actual = tmp_path / "actual-cache"
    actual.mkdir()
    cache = tmp_path / "linked-cache"
    cache.symlink_to(actual, target_is_directory=True)
    root_bytes, fetcher = _signed_repository(_descriptor())
    with pytest.raises(TUFTrustError):
        TUFReleaseTrust(
            cache, tmp_path / "linked-targets",
            "https://control.test.example/agent/v1/tuf/metadata/",
            "https://control.test.example/agent/v1/tuf/targets/",
            root_bytes, fetcher,
            "https://registry.test.example", "dgx/releases", "linux-arm64",
        ).authorize(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )

    target_cache = tmp_path / "target-cache"
    target_cache.mkdir(mode=0o700)
    (target_cache / "spark-runtime-2026-08").symlink_to(tmp_path / "victim")
    root_bytes, fetcher = _signed_repository(_descriptor())
    with pytest.raises(TUFTrustError, match="not empty"):
        TUFReleaseTrust(
            tmp_path / "clean-metadata", target_cache,
            "https://control.test.example/agent/v1/tuf/metadata/",
            "https://control.test.example/agent/v1/tuf/targets/",
            root_bytes, fetcher,
            "https://registry.test.example", "dgx/releases", "linux-arm64",
        ).authorize(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )


def test_tuf_cache_is_single_writer_and_second_updater_fails_closed(tmp_path: Path) -> None:
    root_bytes, base = _signed_repository(_descriptor())
    entered = threading.Event()
    release = threading.Event()

    class BlockingFetcher(RepositoryFetcher):
        def _fetch(self, url: str):
            if url.endswith("timestamp.json"):
                entered.set()
                release.wait(2)
            yield from super()._fetch(url)

    fetcher = BlockingFetcher(base.files)
    arguments = (
        tmp_path / "metadata", tmp_path / "targets",
        "https://control.test.example/agent/v1/tuf/metadata/",
        "https://control.test.example/agent/v1/tuf/targets/",
        root_bytes, fetcher,
        "https://registry.test.example", "dgx/releases", "linux-arm64",
    )
    first = TUFReleaseTrust(*arguments)
    second = TUFReleaseTrust(*arguments)
    errors: list[Exception] = []

    thread = threading.Thread(
        target=lambda: first.authorize(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )
    )
    thread.start()
    assert entered.wait(1)
    with pytest.raises(TUFTrustError, match="already in use"):
        second.authorize(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=1),
        )
    release.set()
    thread.join()
    assert errors == []


def test_tuf_interrupted_refresh_fails_closed_and_same_new_version_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_bytes, initial = _signed_repository(_descriptor(), version=2)

    class InterruptingFetcher(RepositoryFetcher):
        fail_snapshot = False

        def _fetch(self, url: str):
            if self.fail_snapshot and url.endswith("snapshot.json"):
                raise DownloadError("injected interrupted refresh")
            yield from super()._fetch(url)

    fetcher = InterruptingFetcher(initial.files)
    fetcher.signers = initial.signers
    trust = TUFReleaseTrust(
        tmp_path / "metadata", tmp_path / "targets",
        "https://control.test.example/agent/v1/tuf/metadata/",
        "https://control.test.example/agent/v1/tuf/targets/",
        root_bytes, fetcher,
        "https://registry.test.example", "dgx/releases", "linux-arm64",
    )
    request = ReleaseRequest.parse(VALID_RELEASE)
    trust.authorize(request, datetime.now(UTC) + timedelta(seconds=2))
    _, version3 = _signed_repository(
        _descriptor(), version=3, signers=initial.signers, root_bytes=root_bytes
    )
    fetcher.files.update(version3.files)
    fetcher.fail_snapshot = True
    persisted: list[tuple[Path, Path]] = []
    original_persist = update_trust._persist_accepted_cache

    def record_persist(metadata_root, target_root, deadline):
        persisted.append((metadata_root, target_root))
        original_persist(metadata_root, target_root, deadline)

    monkeypatch.setattr(update_trust, "_persist_accepted_cache", record_persist)
    with pytest.raises(TUFTrustError):
        trust.authorize(request, datetime.now(UTC) + timedelta(seconds=2))
    assert persisted == [(tmp_path / "metadata", tmp_path / "targets")]

    fetcher.fail_snapshot = False
    recovered = trust.authorize(
        request, datetime.now(UTC) + timedelta(seconds=2)
    )
    assert recovered.target_digest == "2" * 64


def test_tuf_regular_read_deadline_stops_after_one_slow_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "metadata.json"
    path.write_bytes(b"x" * (3 * 64 * 1024))
    descriptor = os.open(path, os.O_RDONLY)
    original_read = update_trust.os.read
    reads = 0

    def slow_read(fd, size):
        nonlocal reads
        reads += 1
        time.sleep(0.03)
        return original_read(fd, size)

    monkeypatch.setattr(update_trust.os, "read", slow_read)
    deadline = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=1), time.monotonic() + 0.01
    )
    try:
        with pytest.raises(TUFTrustError, match="deadline"):
            update_trust._read_regular_fd(descriptor, 1024 * 1024, deadline)
    finally:
        os.close(descriptor)
    assert reads == 1


def test_tuf_cache_persistence_deadline_stops_remaining_tree_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata = tmp_path / "metadata"
    targets = tmp_path / "targets"
    for root in (metadata, targets):
        root.mkdir(mode=0o700)
        for name in ("one.json", "two.json", "three.json"):
            (root / name).write_bytes(b"x")
    original_chmod = update_trust.os.chmod
    chmods = 0

    def slow_chmod(*args, **kwargs):
        nonlocal chmods
        chmods += 1
        time.sleep(0.03)
        return original_chmod(*args, **kwargs)

    monkeypatch.setattr(update_trust.os, "chmod", slow_chmod)
    deadline = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=1), time.monotonic() + 0.01
    )
    with pytest.raises(TUFTrustError, match="deadline"):
        update_trust._persist_accepted_cache(metadata, targets, deadline)
    assert chmods == 1


def test_tuf_error_persistence_receives_same_expired_deadline_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_bytes, fetcher = _signed_repository(_descriptor())
    trust = TUFReleaseTrust(
        tmp_path / "metadata", tmp_path / "targets",
        "https://control.test.example/agent/v1/tuf/metadata/",
        "https://control.test.example/agent/v1/tuf/targets/",
        root_bytes, fetcher,
        "https://registry.test.example", "dgx/releases", "linux-arm64",
    )
    request = ReleaseRequest.parse(VALID_RELEASE)
    trust.authorize(request, datetime.now(UTC) + timedelta(seconds=2))
    original_persist = update_trust._persist_accepted_cache
    seen: list[MonotonicDeadline] = []
    hardens: list[Path] = []

    def slow_failed_refresh(self):
        time.sleep(0.03)
        raise DownloadError("injected refresh failure")

    def record_persist(metadata_root, target_root, deadline):
        seen.append(deadline)
        return original_persist(metadata_root, target_root, deadline)

    monkeypatch.setattr(update_trust.Updater, "refresh", slow_failed_refresh)
    monkeypatch.setattr(update_trust, "_persist_accepted_cache", record_persist)
    monkeypatch.setattr(
        update_trust, "_harden_cache",
        lambda root, deadline: hardens.append(root),
    )
    deadline = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=1), time.monotonic() + 0.01
    )
    with pytest.raises(TUFTrustError, match="deadline"):
        trust.authorize(request, deadline)
    assert seen == [deadline]
    assert hardens == []


def test_tuf_marker_replace_is_parent_fsynced_before_elapsed_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "metadata"
    root.mkdir(mode=0o700)
    marker = root / ".bootstrap-established"
    clock = [0.0]
    monkeypatch.setattr("dgx_agent.deadlines.time.monotonic", lambda: clock[0])
    original_replace = update_trust.os.replace
    original_fsync = update_trust.os.fsync
    parent_identity = (root.stat().st_dev, root.stat().st_ino)
    parent_synced = False

    def replace_then_expire(source, destination, *args, **kwargs):
        original_replace(source, destination, *args, **kwargs)
        clock[0] = 11.0

    def record_fsync(fd):
        nonlocal parent_synced
        metadata = os.fstat(fd)
        if (metadata.st_dev, metadata.st_ino) == parent_identity:
            parent_synced = True
        return original_fsync(fd)

    monkeypatch.setattr(update_trust.os, "replace", replace_then_expire)
    monkeypatch.setattr(update_trust.os, "fsync", record_fsync)
    deadline = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=10), 10.0
    )

    with pytest.raises(TUFTrustError, match="deadline"):
        update_trust._write_marker(marker, "a" * 64, deadline)

    assert parent_synced
    assert update_trust._marker_root_digest(
        marker,
        MonotonicDeadline(datetime.now(UTC) + timedelta(seconds=10), 20.0),
    ) == "a" * 64


def test_tuf_bootstrap_marker_and_authorization_require_successful_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_bytes, fetcher = _signed_repository(_descriptor())
    metadata = tmp_path / "metadata"
    trust = TUFReleaseTrust(
        metadata, tmp_path / "targets",
        "https://control.test.example/agent/v1/tuf/metadata/",
        "https://control.test.example/agent/v1/tuf/targets/",
        root_bytes, fetcher,
        "https://registry.test.example", "dgx/releases", "linux-arm64",
    )
    request = ReleaseRequest.parse(VALID_RELEASE)
    original = update_trust._fsync_cache
    with monkeypatch.context() as patcher:
        patcher.setattr(
            update_trust, "_fsync_cache",
            lambda root, deadline: (_ for _ in ()).throw(
                OSError("injected fsync")
            ),
        )
        with pytest.raises(TUFTrustError):
            trust.authorize(request, datetime.now(UTC) + timedelta(seconds=2))
    assert not (metadata / ".bootstrap-established").exists()

    calls = 0
    with monkeypatch.context() as patcher:
        def fail_final(root, deadline):
            nonlocal calls
            calls += 1
            original(root, deadline)
            if calls == 2:
                raise OSError("injected final fsync")

        patcher.setattr(update_trust, "_fsync_cache", fail_final)
        with pytest.raises(TUFTrustError):
            trust.authorize(request, datetime.now(UTC) + timedelta(seconds=2))
    assert (metadata / ".bootstrap-established").is_file()
    assert trust.authorize(
        request, datetime.now(UTC) + timedelta(seconds=2)
    ).target_digest == "2" * 64


def test_tuf_recovers_stale_marker_temp_and_missing_root_pointer(tmp_path: Path) -> None:
    root_bytes, fetcher = _signed_repository(_descriptor())
    metadata = tmp_path / "metadata"
    trust = TUFReleaseTrust(
        metadata, tmp_path / "targets",
        "https://control.test.example/agent/v1/tuf/metadata/",
        "https://control.test.example/agent/v1/tuf/targets/",
        root_bytes, fetcher,
        "https://registry.test.example", "dgx/releases", "linux-arm64",
    )
    request = ReleaseRequest.parse(VALID_RELEASE)
    trust.authorize(request, datetime.now(UTC) + timedelta(seconds=2))
    stale = metadata / ".bootstrap-established.new"
    stale.write_text("interrupted")
    stale.chmod(0o600)
    (metadata / "root.json").unlink()

    recovered = trust.authorize(
        request, datetime.now(UTC) + timedelta(seconds=2)
    )

    assert recovered.target_digest == "2" * 64
    assert not stale.exists()
    assert (metadata / "root.json").is_symlink()


def test_tuf_never_bootstrap_rolls_back_when_established_rotated_root_is_lost(
    tmp_path: Path,
) -> None:
    root_bytes, fetcher = _rotated_repository(False)
    metadata = tmp_path / "metadata"
    trust = TUFReleaseTrust(
        metadata, tmp_path / "targets",
        "https://control.test.example/agent/v1/tuf/metadata/",
        "https://control.test.example/agent/v1/tuf/targets/",
        root_bytes, fetcher,
        "https://registry.test.example", "dgx/releases", "linux-arm64",
    )
    request = ReleaseRequest.parse(VALID_RELEASE)
    trust.authorize(request, datetime.now(UTC) + timedelta(seconds=2))
    root_target = os.readlink(metadata / "root.json")
    assert root_target == "root_history/2.root.json"
    (metadata / "root.json").unlink()
    (metadata / root_target).unlink()
    fetcher.urls.clear()

    with pytest.raises(TUFTrustError, match="operator recovery"):
        trust.authorize(request, datetime.now(UTC) + timedelta(seconds=2))

    assert fetcher.urls == []
    assert (metadata / ".bootstrap-established").is_file()


def test_bounded_https_fetcher_accepts_only_exact_tuf_routes_and_deadline() -> None:
    class Response:
        status = 200

        def __init__(self):
            self.parts = [b"signed", b""]

        def read(self, amount):
            return self.parts.pop(0)

        def release_conn(self):
            pass

    class Pool:
        def request(self, *args, **kwargs):
            return Response()

    fetcher = BoundedHTTPSFetcher(
        "https://control.test.example", ssl.create_default_context(), pool=Pool()
    )
    fetcher.set_deadline(time.monotonic() + 1)
    assert fetcher.download_bytes(
        "https://control.test.example/agent/v1/tuf/metadata/timestamp.json", 64
    ) == b"signed"
    for url in (
        "https://attacker.test/agent/v1/tuf/metadata/timestamp.json",
        "https://control.test.example/agent/v1/tuf/metadata/../secret",
        "https://control.test.example/agent/v1/tuf/targets/a?tag=latest",
    ):
        with pytest.raises(DownloadError):
            fetcher.download_bytes(url, 64)

def _oras_policy(tmp_path: Path) -> tuple[ORASPolicy, Path]:
    record = tmp_path / "oras-record.json"
    executable = tmp_path / "oras"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "fd_args = [sys.argv[sys.argv.index(flag) + 1] for flag in ('--registry-config', '--ca-file', '--cert-file', '--key-file')]\n"
        f"pathlib.Path({str(record)!r}).write_text(json.dumps({{'argv': sys.argv, 'env': dict(os.environ), 'credentials': [pathlib.Path(path).read_text() for path in fd_args]}}))\n"
        "output = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "member = output / 'bin/runtime-adapter'\n"
        "member.parent.mkdir(parents=True, exist_ok=True)\n"
        "member.write_bytes(b'x' * 17)\n"
        "member.chmod(0o500)\n"
    )
    executable.chmod(0o755)
    files = {}
    for name, mode in (("auth.json", 0o600), ("ca.pem", 0o644), ("client.pem", 0o644), ("client.key", 0o600)):
        path = tmp_path / name
        path.write_text(name)
        path.chmod(mode)
        files[name] = path
    policy = ORASPolicy(
        registry_origin="https://registry.test.example",
        repository="dgx/releases",
        executable=executable,
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        executable_version="1.3.3",
        auth_path=files["auth.json"],
        ca_path=files["ca.pem"],
        client_certificate_path=files["client.pem"],
        client_key_path=files["client.key"],
        allow_unprivileged_test_files=True,
    )
    return policy, record


def test_oras_uses_only_digest_reference_fixed_files_and_fixed_environment(tmp_path: Path) -> None:
    policy, record = _oras_policy(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    descriptor = ReleaseDescriptor.parse(_descriptor())

    ORASClient(policy).pull(
        descriptor, staging, datetime.now(UTC) + timedelta(seconds=2)
    )

    invocation = json.loads(record.read_text())
    assert invocation["argv"][0].startswith("/proc/self/fd/")
    assert invocation["argv"][1:] == [
        "pull",
        "registry.test.example/dgx/releases@sha256:" + "1" * 64,
        "--output",
        str(staging),
        "--registry-config",
        invocation["argv"][6],
        "--ca-file",
        invocation["argv"][8],
        "--cert-file",
        invocation["argv"][10],
        "--key-file",
        invocation["argv"][12],
        "--concurrency",
        "2",
    ]
    assert set(invocation["env"]) == {
        "LANG", "LC_ALL", "PATH", "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE"
    }
    assert "HTTP_PROXY" not in invocation["env"]
    for value in invocation["argv"][6:13:2]:
        assert value.startswith("/proc/self/fd/")
    assert invocation["credentials"] == [
        "auth.json", "ca.pem", "client.pem", "client.key"
    ]


def test_production_private_oras_file_accepts_only_service_uid_exact_0600(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_uid = 42424
    metadata = type("Metadata", (), {
        "st_mode": stat.S_IFREG | 0o600,
        "st_nlink": 1,
        "st_uid": service_uid,
    })()
    monkeypatch.setattr(oci_module.os, "geteuid", lambda: service_uid)

    oci_module._trusted_policy_file(
        metadata, private=True, allow_unprivileged_test_files=False
    )
    with pytest.raises(OCIError):
        oci_module._trusted_policy_file(
            metadata, private=False, allow_unprivileged_test_files=False
        )
    for mode in (0o400, 0o640, 0o600 | stat.S_ISUID):
        changed = type("Metadata", (), {
            "st_mode": stat.S_IFREG | mode,
            "st_nlink": 1,
            "st_uid": service_uid,
        })()
        with pytest.raises(OCIError):
            oci_module._trusted_policy_file(
                changed, private=True, allow_unprivileged_test_files=False
            )


def test_production_private_oras_snapshot_opens_service_file_below_root_ancestry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = tmp_path / "auth.json"
    auth.write_bytes(b'{"auths":{}}')
    auth.chmod(0o600)
    original_fstat = oci_module.os.fstat

    def deployed_metadata(fd):
        metadata = original_fstat(fd)
        if stat.S_ISDIR(metadata.st_mode):
            return type("DirectoryMetadata", (), {
                "st_mode": stat.S_IFDIR | 0o755,
                "st_uid": 0,
            })()
        return metadata

    monkeypatch.setattr(oci_module.os, "fstat", deployed_metadata)
    snapshot = oci_module._snapshot_policy_file(
        auth, private=True, allow_unprivileged_test_files=False
    )
    try:
        assert os.read(snapshot, 64) == b'{"auths":{}}'
    finally:
        os.close(snapshot)


def test_oras_uses_sealed_credential_snapshots_after_path_and_inode_mutation(tmp_path: Path) -> None:
    policy, record = _oras_policy(tmp_path)
    client = ORASClient(policy)
    policy.auth_path.unlink()
    policy.auth_path.write_text("attacker replacement")
    policy.auth_path.chmod(0o600)
    policy.ca_path.chmod(0o600)
    policy.ca_path.write_text("same inode mutation")
    policy.ca_path.chmod(0o644)
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)

    client.pull(
        ReleaseDescriptor.parse(_descriptor()),
        staging,
        datetime.now(UTC) + timedelta(seconds=2),
    )

    invocation = json.loads(record.read_text())
    assert invocation["credentials"][:2] == ["auth.json", "ca.pem"]


def test_oras_rejects_policy_mismatch_before_launch(tmp_path: Path) -> None:
    policy, record = _oras_policy(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    changed = _descriptor() | {"repository": "other/releases"}

    with pytest.raises(OCIError):
        ORASClient(policy).pull(
            ReleaseDescriptor.parse(changed),
            staging,
            datetime.now(UTC) + timedelta(seconds=2),
        )
    assert not record.exists()


def test_oras_close_waits_for_active_pull_and_then_fails_closed(tmp_path: Path) -> None:
    policy, _ = _oras_policy(tmp_path)
    client = ORASClient(policy)
    entered = threading.Event()
    release = threading.Event()

    class Runner:
        def run(self, request):
            entered.set()
            release.wait(2)
            return ProcessOutcome(0, b"", b"")

    client._runner = Runner()
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    errors: list[Exception] = []

    def pull():
        try:
            client.pull(
                ReleaseDescriptor.parse(_descriptor()), staging,
                datetime.now(UTC) + timedelta(seconds=2),
            )
        except Exception as error:
            errors.append(error)

    pull_thread = threading.Thread(target=pull)
    close_thread = threading.Thread(target=client.close)
    pull_thread.start()
    assert entered.wait(1)
    close_thread.start()
    time.sleep(0.02)
    assert close_thread.is_alive()
    release.set()
    pull_thread.join()
    close_thread.join()
    assert errors == []
    with pytest.raises(OCIError, match="closed"):
        client.pull(
            ReleaseDescriptor.parse(_descriptor()), staging,
            datetime.now(UTC) + timedelta(seconds=1),
        )


def test_release_install_is_atomic_verified_and_idempotent(tmp_path: Path) -> None:
    root_bytes, fetcher = _signed_repository(_descriptor())
    trust = TUFReleaseTrust(
        tmp_path / "metadata", tmp_path / "targets",
        "https://control.test.example/agent/v1/tuf/metadata/",
        "https://control.test.example/agent/v1/tuf/targets/",
        root_bytes, fetcher,
        "https://registry.test.example", "dgx/releases", "linux-arm64",
    )
    policy, record = _oras_policy(tmp_path)
    releases_root = tmp_path / "release-store"
    staging_root = tmp_path / "release-staging"
    installer = ReleaseInstaller(
        trust, ORASClient(policy), releases_root, staging_root
    )
    request = ReleaseRequest.parse(VALID_RELEASE)

    first = installer.install(request, datetime.now(UTC) + timedelta(seconds=2))
    first_invocation = record.read_bytes()
    second = installer.install(request, datetime.now(UTC) + timedelta(seconds=2))

    installed = releases_root / ("2" * 64)
    assert first.status == "installed"
    assert second.status == "already-installed"
    assert (installed / "bin/runtime-adapter").read_bytes() == b"x" * 17
    assert (installed / ".install-receipt.json").is_file()
    assert record.read_bytes() == first_invocation
    assert list(staging_root.iterdir()) == []


def test_installed_verification_rejects_destination_swap_between_receipt_and_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())
    destination = tmp_path / "installed"
    replacement = tmp_path / "replacement"
    moved = tmp_path / "moved"
    for root in (destination, replacement):
        member = root / "bin/runtime-adapter"
        member.parent.mkdir(parents=True, mode=0o700)
        member.write_bytes(b"x" * 17)
        member.chmod(0o500)
        release_module._write_receipt(root, descriptor)

    original_loads = release_module.json.loads
    swapped = False

    def loads_then_swap(raw, **kwargs):
        nonlocal swapped
        document = original_loads(raw, **kwargs)
        if not swapped:
            swapped = True
            os.rename(destination, moved)
            os.rename(replacement, destination)
        return document

    monkeypatch.setattr(release_module.json, "loads", loads_then_swap)

    with pytest.raises(ReleaseInstallError, match="identity"):
        release_module._verify_installed(
            destination.parent, destination.name, descriptor
        )

    assert destination.stat().st_ino != moved.stat().st_ino


@pytest.mark.parametrize("mutation", ["reordered", "duplicate", "trailing"])
def test_installed_receipt_requires_duplicate_free_canonical_bytes(
    tmp_path: Path, mutation: str
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())
    root = tmp_path / "installed"
    member = root / "bin/runtime-adapter"
    member.parent.mkdir(parents=True, mode=0o700)
    member.write_bytes(b"x" * 17)
    member.chmod(0o500)
    release_module._write_receipt(root, descriptor)
    receipt = root / ".install-receipt.json"
    canonical = release_module._receipt_bytes(descriptor)
    assert verify_installed_release(root) == descriptor

    if mutation == "reordered":
        raw = json.dumps(
            {"release": descriptor.to_mapping(), "schema_version": 1},
            indent=2,
        ).encode() + b"\n"
    elif mutation == "duplicate":
        raw = (
            b'{"schema_version":1,"schema_version":1,"release":'
            + json.dumps(
                descriptor.to_mapping(), sort_keys=True, separators=(",", ":")
            ).encode()
            + b"}\n"
        )
    else:
        raw = canonical + b"\n"
    receipt.chmod(0o600)
    receipt.write_bytes(raw)
    receipt.chmod(0o400)

    with pytest.raises(ReleaseInstallError, match="receipt"):
        verify_installed_release(root)


def test_release_member_deadline_stops_after_one_blocking_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"x" * (3 * 64 * 1024)
    document = _descriptor()
    document["target_length"] = len(content)
    document["members"][0]["size"] = len(content)
    document["members"][0]["sha256"] = hashlib.sha256(content).hexdigest()
    descriptor = ReleaseDescriptor.parse(document)
    root = tmp_path / "tree"
    member = root / "bin/runtime-adapter"
    member.parent.mkdir(parents=True, mode=0o700)
    member.write_bytes(content)
    member.chmod(0o500)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    original_read = release_module.os.read
    reads = 0

    def slow_read(fd, size):
        nonlocal reads
        reads += 1
        time.sleep(0.03)
        return original_read(fd, size)

    monkeypatch.setattr(release_module.os, "read", slow_read)
    deadline = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=1), time.monotonic() + 0.01
    )
    try:
        with pytest.raises(ReleaseInstallError, match="deadline"):
            release_module._verify_release_tree_fd(
                root_fd, descriptor, deadline=deadline
            )
    finally:
        os.close(root_fd)
    assert reads == 1


def test_release_recursive_fsync_deadline_stops_after_crossing_syscall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    for name in ("a/one", "b/two", "c/three"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    fsyncs = 0

    def slow_fsync(fd):
        nonlocal fsyncs
        fsyncs += 1
        time.sleep(0.03)

    monkeypatch.setattr(release_module.os, "fsync", slow_fsync)
    deadline = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=1), time.monotonic() + 0.01
    )
    try:
        with pytest.raises(ReleaseInstallError, match="deadline"):
            release_module._fsync_tree_fd(root_fd, deadline)
    finally:
        os.close(root_fd)
    assert fsyncs == 1


def test_release_rename_is_parent_fsynced_before_elapsed_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    class Transport:
        def pull(self, descriptor, destination, deadline):
            member = destination / "bin/runtime-adapter"
            member.parent.mkdir()
            member.write_bytes(b"x" * 17)
            member.chmod(0o500)

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    installer = ReleaseInstaller(Trust(), Transport(), releases, staging)
    original_rename = release_module._rename_noreplace
    original_fsync = release_module.os.fsync
    clock = [0.0]
    parent_synced = False
    monkeypatch.setattr("dgx_agent.deadlines.time.monotonic", lambda: clock[0])

    def rename_then_expire(source, destination):
        original_rename(source, destination)
        clock[0] = 11.0

    def record_fsync(fd):
        nonlocal parent_synced
        metadata = os.fstat(fd)
        if releases.exists():
            root_metadata = releases.stat()
            if (metadata.st_dev, metadata.st_ino) == (
                root_metadata.st_dev, root_metadata.st_ino
            ):
                parent_synced = True
        return original_fsync(fd)

    monkeypatch.setattr(release_module, "_rename_noreplace", rename_then_expire)
    monkeypatch.setattr(release_module.os, "fsync", record_fsync)
    request = ReleaseRequest.parse(VALID_RELEASE)
    deadline = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=10), 10.0
    )

    with pytest.raises(ReleaseInstallError, match="deadline"):
        installer.install(request, deadline)

    assert parent_synced
    assert (releases / ("2" * 64)).is_dir()
    clock[0] = 0.0
    assert installer.install(
        request,
        MonotonicDeadline(datetime.now(UTC) + timedelta(seconds=10), 10.0),
    ).status == "already-installed"


@pytest.mark.parametrize("branch", ["initial-existing", "rename-race", "inspect"])
def test_every_idempotent_branch_rejects_destination_identity_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, branch: str
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    class Transport:
        def pull(self, descriptor, destination, deadline):
            member = destination / "bin/runtime-adapter"
            member.parent.mkdir()
            member.write_bytes(b"x" * 17)
            member.chmod(0o500)

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    releases.mkdir(mode=0o700)
    staging.mkdir(mode=0o700)
    destination = releases / ("2" * 64)
    replacement = tmp_path / "replacement"
    moved = tmp_path / "moved"

    def make_installed(root: Path) -> None:
        member = root / "bin/runtime-adapter"
        member.parent.mkdir(parents=True, mode=0o700)
        member.write_bytes(b"x" * 17)
        member.chmod(0o500)
        release_module._write_receipt(root, descriptor)

    if branch != "rename-race":
        make_installed(destination)
        make_installed(replacement)

    original_loads = release_module.json.loads
    swapped = False

    def loads_then_swap(raw, **kwargs):
        nonlocal swapped
        document = original_loads(raw, **kwargs)
        if not swapped and destination.exists() and replacement.exists():
            swapped = True
            os.rename(destination, moved)
            os.rename(replacement, destination)
        return document

    monkeypatch.setattr(release_module.json, "loads", loads_then_swap)
    installer = ReleaseInstaller(Trust(), Transport(), releases, staging)
    request = ReleaseRequest.parse(VALID_RELEASE)
    deadline = datetime.now(UTC) + timedelta(seconds=2)

    if branch == "rename-race":
        def competing_publish(source, target):
            make_installed(destination)
            make_installed(replacement)
            raise FileExistsError(target)

        monkeypatch.setattr(
            release_module, "_rename_noreplace", competing_publish
        )
        with pytest.raises(ReleaseInstallError, match="identity"):
            installer.install(request, deadline)
    elif branch == "initial-existing":
        with pytest.raises(ReleaseInstallError, match="identity"):
            installer.install(request, deadline)
    else:
        assert (
            installer.inspect(request, deadline).disposition
            is ReleaseDisposition.OPERATOR_INTERVENTION
        )

    assert swapped
    assert destination.stat().st_ino != moved.stat().st_ino


@pytest.mark.parametrize(
    "attack",
    ["unexpected", "symlink", "hardlink", "fifo", "mode", "case-collision"],
)
def test_release_tree_rejects_untrusted_member_attacks(tmp_path: Path, attack: str) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())
    root = tmp_path / "tree"
    member = root / "bin/runtime-adapter"
    member.parent.mkdir(parents=True)
    member.write_bytes(b"x" * 17)
    member.chmod(0o500)
    if attack == "unexpected":
        (root / "extra").write_text("x")
    elif attack == "symlink":
        member.unlink()
        member.symlink_to("/etc/passwd")
    elif attack == "hardlink":
        os.link(member, tmp_path / "outside-link")
    elif attack == "fifo":
        member.unlink()
        os.mkfifo(member)
    elif attack == "mode":
        member.chmod(0o700)
    else:
        collision = root / "BIN/runtime-adapter"
        collision.parent.mkdir()
        collision.write_bytes(b"x" * 17)

    with pytest.raises(ReleaseInstallError):
        verify_release_tree(root, descriptor)


@pytest.mark.parametrize(
    "path",
    [
        "/bin/runtime-adapter",
        "../runtime-adapter",
        "bin\\runtime-adapter",
        "bin/e\u0301-adapter",
    ],
)
def test_descriptor_rejects_absolute_parent_backslash_and_non_nfc_paths(path: str) -> None:
    document = _descriptor()
    document["members"][0]["path"] = path
    with pytest.raises(ReleaseValidationError):
        ReleaseDescriptor.parse(document)


@pytest.mark.parametrize("second_path", ["bin/runtime-adapter", "BIN/runtime-adapter"])
def test_descriptor_rejects_duplicate_and_casefolded_member_identity(second_path: str) -> None:
    document = _descriptor()
    document["target_length"] = 34
    document["members"].append(document["members"][0] | {"path": second_path})
    document["members"].sort(key=lambda item: item["path"])
    with pytest.raises(ReleaseValidationError):
        ReleaseDescriptor.parse(document)


@pytest.mark.parametrize("limit", ["count", "file", "aggregate"])
def test_descriptor_rejects_member_count_file_and_aggregate_limits(limit: str) -> None:
    document = _descriptor()
    if limit == "count":
        document["members"] = [
            document["members"][0]
            | {"path": f"bin/member-{index:03d}", "size": 0}
            for index in range(257)
        ]
        document["members"][0]["size"] = 1
        document["target_length"] = 1
    elif limit == "file":
        document["members"][0]["size"] = 256 * 1024 * 1024 + 1
        document["target_length"] = 1
    else:
        document["members"] = [
            document["members"][0]
            | {"path": f"bin/member-{index}", "size": 256 * 1024 * 1024}
            for index in range(5)
        ]
        document["target_length"] = 1
    with pytest.raises(ReleaseValidationError):
        ReleaseDescriptor.parse(document)


def test_descriptor_rejects_a_receipt_that_cannot_be_reverified() -> None:
    document = _descriptor()
    document["members"] = [
        document["members"][0]
        | {"path": f"bin/{index:03d}-" + "a" * 300, "size": 0}
        for index in range(256)
    ]
    document["members"][0]["size"] = 1
    document["target_length"] = 1
    with pytest.raises(ReleaseValidationError, match="receipt"):
        ReleaseDescriptor.parse(document)


@pytest.mark.parametrize("owner_field", ["uid", "gid"])
def test_release_tree_rejects_wrong_signed_owner(tmp_path: Path, owner_field: str) -> None:
    document = _descriptor()
    document["members"][0][owner_field] = (
        document["members"][0][owner_field] + 1
    ) % 65536
    descriptor = ReleaseDescriptor.parse(document)
    member = tmp_path / "tree/bin/runtime-adapter"
    member.parent.mkdir(parents=True)
    member.write_bytes(b"x" * 17)
    member.chmod(0o500)
    with pytest.raises(ReleaseInstallError):
        verify_release_tree(tmp_path / "tree", descriptor)


def test_release_tree_rejects_unix_socket_and_device_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())
    root = tmp_path / "tree"
    member = root / "bin/runtime-adapter"
    member.parent.mkdir(parents=True)
    unix_socket = socket.socket(socket.AF_UNIX)
    try:
        unix_socket.bind(str(member))
        with pytest.raises(ReleaseInstallError):
            verify_release_tree(root, descriptor)
    finally:
        unix_socket.close()
    member.unlink()
    member.write_bytes(b"x" * 17)
    member.chmod(0o500)
    original_open = release_module.os.open

    def device_open(path, flags, *args, **kwargs):
        if path == "runtime-adapter" and kwargs.get("dir_fd") is not None:
            return original_open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(release_module.os, "open", device_open)
    with pytest.raises(ReleaseInstallError):
        verify_release_tree(root, descriptor)


def test_release_tree_rejects_unsafe_directory_mode_and_sparse_member(tmp_path: Path) -> None:
    descriptor_document = _descriptor()
    descriptor_document["target_length"] = 1024 * 1024
    descriptor_document["members"][0]["size"] = 1024 * 1024
    descriptor_document["members"][0]["sha256"] = hashlib.sha256(
        b"\0" * (1024 * 1024)
    ).hexdigest()
    descriptor = ReleaseDescriptor.parse(descriptor_document)
    root = tmp_path / "tree"
    member = root / "bin/runtime-adapter"
    member.parent.mkdir(parents=True)
    with member.open("wb") as stream:
        stream.truncate(1024 * 1024)
    member.chmod(0o500)

    with pytest.raises(ReleaseInstallError):
        verify_release_tree(root, descriptor)

    member.chmod(0o600)
    member.write_bytes(b"\0" * (1024 * 1024))
    member.chmod(0o500)
    member.parent.chmod(0o777)
    with pytest.raises(ReleaseInstallError):
        verify_release_tree(root, descriptor)


def test_installer_never_replaces_a_dangling_preexisting_destination(tmp_path: Path) -> None:
    class Trust:
        def authorize(self, request, deadline):
            return ReleaseDescriptor.parse(_descriptor())

    class Transport:
        called = False

        def pull(self, descriptor, destination, deadline):
            self.called = True

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    releases.mkdir(mode=0o700)
    staging.mkdir(mode=0o700)
    destination = releases / ("2" * 64)
    destination.symlink_to(tmp_path / "missing")
    transport = Transport()

    with pytest.raises(ReleaseInstallError):
        ReleaseInstaller(Trust(), transport, releases, staging).install(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )

    assert destination.is_symlink()
    assert not transport.called


def test_installer_serializes_same_release_transport(tmp_path: Path) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    class Transport:
        def __init__(self):
            self.active = 0
            self.maximum = 0
            self.calls = 0
            self.guard = threading.Lock()

        def pull(self, descriptor, destination, deadline):
            with self.guard:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                self.calls += 1
            member = destination / "bin/runtime-adapter"
            member.parent.mkdir()
            member.write_bytes(b"x" * 17)
            member.chmod(0o500)
            time.sleep(0.05)
            with self.guard:
                self.active -= 1

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    transport = Transport()
    installer = ReleaseInstaller(Trust(), transport, releases, staging)
    request = ReleaseRequest.parse(VALID_RELEASE)
    errors: list[Exception] = []

    def install() -> None:
        try:
            installer.install(request, datetime.now(UTC) + timedelta(seconds=2))
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=install) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert transport.maximum == 1
    assert transport.calls == 1


def test_installer_lock_wait_is_bounded_by_claim_deadline(tmp_path: Path) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    class Transport:
        called = False

        def pull(self, descriptor, destination, deadline):
            self.called = True

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    releases.mkdir(mode=0o700)
    staging.mkdir(mode=0o700)
    lock = os.open(
        releases / (".install-" + "2" * 64 + ".lock"),
        os.O_RDWR | os.O_CREAT,
        0o600,
    )
    fcntl.flock(lock, fcntl.LOCK_EX)
    started = time.monotonic()
    try:
        with pytest.raises(ReleaseInstallError, match="deadline"):
            ReleaseInstaller(Trust(), Transport(), releases, staging).install(
                ReleaseRequest.parse(VALID_RELEASE),
                datetime.now(UTC) + timedelta(milliseconds=50),
            )
    finally:
        os.close(lock)
    assert time.monotonic() - started < 0.5


def test_monotonic_deadline_cannot_be_extended_by_backward_wall_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed = MonotonicDeadline.bind(datetime.now(UTC) + timedelta(seconds=1))

    class BackwardClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2000, 1, 1, tzinfo=UTC)

    monkeypatch.setattr("dgx_agent.deadlines.datetime", BackwardClock)
    monkeypatch.setattr(
        "dgx_agent.deadlines.time.monotonic",
        lambda: fixed.absolute_monotonic + 0.001,
    )
    with pytest.raises(DeadlineBindingError):
        fixed.check()


def test_slow_trust_stage_cannot_start_transport_after_total_deadline(tmp_path: Path) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            time.sleep(0.03)
            return descriptor

    class Transport:
        called = False

        def pull(self, descriptor, destination, deadline):
            self.called = True

    transport = Transport()
    fixed = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=1), time.monotonic() + 0.01
    )
    with pytest.raises(ReleaseInstallError, match="deadline"):
        ReleaseInstaller(
            Trust(), transport, tmp_path / "releases", tmp_path / "staging"
        ).install(ReleaseRequest.parse(VALID_RELEASE), fixed)
    assert not transport.called


def test_release_inspection_considers_only_matching_digest_staging(tmp_path: Path) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    class Transport:
        def pull(self, descriptor, destination, deadline):
            raise AssertionError("inspection must not pull")

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    releases.mkdir(mode=0o700)
    staging.mkdir(mode=0o700)
    other = staging / (".install-" + "9" * 64 + "-stale")
    member = other / "bin/runtime-adapter"
    member.parent.mkdir(parents=True)
    member.write_bytes(b"x" * 17)
    member.chmod(0o500)
    installer = ReleaseInstaller(Trust(), Transport(), releases, staging)
    request = ReleaseRequest.parse(VALID_RELEASE)

    assert installer.inspect(
        request, datetime.now(UTC) + timedelta(seconds=2)
    ).disposition is ReleaseDisposition.OPERATOR_INTERVENTION

    matching = staging / (".install-" + "2" * 64 + "-resume")
    matching_member = matching / "bin/runtime-adapter"
    matching_member.parent.mkdir(parents=True)
    matching_member.write_bytes(b"x" * 17)
    matching_member.chmod(0o500)
    assert installer.inspect(
        request, datetime.now(UTC) + timedelta(seconds=2)
    ).disposition is ReleaseDisposition.SAFE_TO_RESUME


def test_publication_detects_but_never_deletes_foreign_staging_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    class Transport:
        def pull(self, descriptor, destination, deadline):
            member = destination / "bin/runtime-adapter"
            member.parent.mkdir()
            member.write_bytes(b"x" * 17)
            member.chmod(0o500)

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    original_rename = release_module._rename_noreplace

    def substitute_then_rename(source, destination):
        backup = source.with_name(".attacker-moved-verified-tree")
        os.rename(source, backup)
        source.mkdir(mode=0o700)
        malicious = source / "bin/runtime-adapter"
        malicious.parent.mkdir()
        malicious.write_bytes(b"attacker")
        malicious.chmod(0o500)
        original_rename(source, destination)

    monkeypatch.setattr(
        release_module, "_rename_noreplace", substitute_then_rename
    )
    with pytest.raises(ReleaseInstallError, match="identity"):
        ReleaseInstaller(Trust(), Transport(), releases, staging).install(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )

    foreign = releases / ("2" * 64)
    assert (foreign / "bin/runtime-adapter").read_bytes() == b"attacker"
    with pytest.raises(ReleaseInstallError):
        ReleaseInstaller(Trust(), Transport(), releases, staging).install(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )
