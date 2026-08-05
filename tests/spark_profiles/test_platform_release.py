from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from securesystemslib.signer import CryptoSigner
from tuf.api.exceptions import DownloadHTTPError
from tuf.api.metadata import (
    Metadata,
    MetaFile,
    Root,
    Snapshot,
    TargetFile,
    Targets,
    Timestamp,
)
from tuf.ngclient import FetcherInterface

from spark_profiles.platform_release import (
    PlatformIdentity,
    PlatformRelease,
    PlatformReleaseError,
)
from spark_profiles.update_trust import UpdateTrust, UpdateTrustError

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


def _artifact(name: str, digest: str) -> dict[str, object]:
    return {
        "name": name,
        "reference": f"ghcr.io/example/dgx-forge/{name}@sha256:{digest}",
        "sha256": digest,
        "size": 1024,
        "sbom_sha256": SHA_D,
        "provenance_sha256": SHA_E,
    }


def _manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "platform_version": "1.2.0",
        "build_digest": f"sha256:{SHA_A}",
        "control": {
            "config_version": 3,
            "protocol": {"minimum": 2, "maximum": 3},
            "images": {
                "api": _artifact("api", SHA_A),
                "worker": _artifact("worker", SHA_B),
            },
            "assets": [_artifact("web", SHA_C)],
        },
        "database": {
            "expand_revision": "0010_update_rollouts",
            "contract_revision": None,
            "predecessor_compatible": True,
        },
        "agents": [
            {
                "architecture": "linux-arm64",
                "protocol": {"minimum": 1, "maximum": 2},
                "artifact": _artifact("agent-linux-arm64", SHA_A),
            }
        ],
        "supervisors": [
            {
                "architecture": "linux-arm64",
                "artifact": _artifact("supervisor-linux-arm64", SHA_B),
            }
        ],
        "tooling": [
            {
                "architecture": "linux-arm64",
                "artifact": _artifact("tooling-linux-arm64", SHA_C),
            }
        ],
        "rollback": {
            "compatible_predecessor_builds": [f"sha256:{SHA_B}"],
        },
    }


def _write(tmp_path: Path, document: dict[str, object], name: str = "release.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_platform_release_loads_strict_typed_contract(tmp_path: Path) -> None:
    release = PlatformRelease.load(_write(tmp_path, _manifest()))

    assert release.platform_version == "1.2.0"
    assert release.build_digest == f"sha256:{SHA_A}"
    assert release.control.config_version == 3
    assert release.agent_for("linux-arm64").protocol.minimum == 1
    assert release.digest.startswith("sha256:")
    assert len(release.digest) == 71


def test_platform_update_schema_is_packaged_and_matches_repository_copy() -> None:
    repository = (
        Path(__file__).resolve().parents[2]
        / "schemas/platform-update-manifest.schema.json"
    ).read_bytes()
    packaged = (
        resources.files("spark_profiles")
        .joinpath("schemas", "platform-update-manifest.schema.json")
        .read_bytes()
    )

    assert packaged == repository


def test_platform_release_digest_is_canonical_under_object_key_reordering(
    tmp_path: Path,
) -> None:
    original = _manifest()
    reordered = dict(reversed(list(original.items())))
    reordered["control"] = dict(
        reversed(list(copy.deepcopy(original["control"]).items()))  # type: ignore[union-attr]
    )

    first = PlatformRelease.load(_write(tmp_path, original, "first.json"))
    second = PlatformRelease.load(_write(tmp_path, reordered, "second.json"))

    assert first.digest == second.digest


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=True),
        lambda d: d.update(platform_version="v1.2"),
        lambda d: d.update(build_digest=SHA_A),
        lambda d: d["control"]["images"]["api"].update(  # type: ignore[index,union-attr]
            reference="ghcr.io/example/dgx-forge/api:latest"
        ),
        lambda d: d["control"]["images"]["api"].update(  # type: ignore[index,union-attr]
            sbom_sha256=None
        ),
        lambda d: d["control"].update(protocol={"minimum": 3, "maximum": 2}),  # type: ignore[union-attr]
        lambda d: d["agents"].append(copy.deepcopy(d["agents"][0])),  # type: ignore[union-attr,index]
        lambda d: d["database"].update(  # type: ignore[union-attr]
            contract_revision="0012_contract", predecessor_compatible=False
        ),
        lambda d: d["rollback"].update(compatible_predecessor_builds=[]),  # type: ignore[union-attr]
    ],
    ids=[
        "unknown-field",
        "invalid-semver",
        "invalid-build-digest",
        "floating-image",
        "missing-sbom",
        "invalid-protocol-range",
        "overlapping-architecture",
        "destructive-migration-without-predecessor-compatibility",
        "missing-recovery-predecessor",
    ],
)
def test_platform_release_rejects_unsafe_or_ambiguous_inputs(
    tmp_path: Path, mutate: object
) -> None:
    document = _manifest()
    mutate(document)  # type: ignore[operator]

    with pytest.raises(PlatformReleaseError):
        PlatformRelease.load(_write(tmp_path, document))


def test_artifact_reference_digest_must_match_bound_sha256(tmp_path: Path) -> None:
    document = _manifest()
    document["control"]["images"]["api"]["sha256"] = SHA_C  # type: ignore[index]

    with pytest.raises(PlatformReleaseError, match="reference digest"):
        PlatformRelease.load(_write(tmp_path, document))


def test_compatibility_requires_architecture_protocol_overlap_and_rollback(
    tmp_path: Path,
) -> None:
    release = PlatformRelease.load(_write(tmp_path, _manifest()))
    compatible = PlatformIdentity(
        platform_version="1.1.0",
        build_digest=f"sha256:{SHA_B}",
        architecture="linux-arm64",
        control_api_protocol=2,
        agent_protocol=1,
    )

    report = release.compatibility(compatible)

    assert report.compatible is True
    assert report.update_recommended is True
    assert report.reasons == ()

    incompatible = PlatformIdentity(
        platform_version="1.1.0",
        build_digest=f"sha256:{SHA_C}",
        architecture="linux-x86_64",
        control_api_protocol=1,
        agent_protocol=7,
    )
    rejected = release.compatibility(incompatible)
    assert rejected.compatible is False
    assert set(rejected.reasons) == {
        "architecture-not-published",
        "control-protocol-incompatible",
        "predecessor-not-recovery-compatible",
    }


def test_same_build_is_current_and_not_an_update(tmp_path: Path) -> None:
    release = PlatformRelease.load(_write(tmp_path, _manifest()))
    current = PlatformIdentity(
        platform_version="1.2.0",
        build_digest=f"sha256:{SHA_A}",
        architecture="linux-arm64",
        control_api_protocol=2,
        agent_protocol=1,
    )

    report = release.compatibility(current)

    assert report.compatible is True
    assert report.update_recommended is False


class _RepositoryFetcher(FetcherInterface):
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.urls: list[str] = []

    def _fetch(self, url: str):
        self.urls.append(url)
        name = urlsplit(url).path.rsplit("/", 1)[-1]
        try:
            yield self.files[name]
        except KeyError as error:
            raise DownloadHTTPError("missing", 404) from error


def _signed_repository(
    target_bytes: bytes,
    *,
    expired: bool = False,
    version: int = 1,
    signers: dict[str, CryptoSigner] | None = None,
    root_bytes: bytes | None = None,
) -> tuple[bytes, _RepositoryFetcher]:
    expiry = datetime.now(UTC) + (-timedelta(days=1) if expired else timedelta(days=1))
    signers = signers or {
        role: CryptoSigner.generate_ed25519()
        for role in ("root", "timestamp", "snapshot", "targets")
    }
    if root_bytes is None:
        root = Root(
            expires=datetime.now(UTC) + timedelta(days=1),
            consistent_snapshot=False,
        )
        for role, signer in signers.items():
            root.add_key(signer.public_key, role)
        root_metadata = Metadata(root)
        root_metadata.sign(signers["root"])
        root_bytes = root_metadata.to_bytes()
    target = TargetFile.from_data("platform-release.json", target_bytes, ["sha256"])
    targets = Metadata(
        Targets(
            version=version,
            expires=expiry,
            targets={"platform-release.json": target},
        )
    )
    targets.sign(signers["targets"])
    targets_bytes = targets.to_bytes()
    snapshot = Metadata(
        Snapshot(
            version=version,
            expires=expiry,
            meta={"targets.json": MetaFile.from_data(version, targets_bytes, ["sha256"])},
        )
    )
    snapshot.sign(signers["snapshot"])
    snapshot_bytes = snapshot.to_bytes()
    timestamp = Metadata(
        Timestamp(
            version=version,
            expires=expiry,
            snapshot_meta=MetaFile.from_data(version, snapshot_bytes, ["sha256"]),
        )
    )
    timestamp.sign(signers["timestamp"])
    fetcher = _RepositoryFetcher(
        {
            "timestamp.json": timestamp.to_bytes(),
            "snapshot.json": snapshot_bytes,
            "targets.json": targets_bytes,
            "platform-release.json": target_bytes,
        }
    )
    fetcher.signers = signers
    return root_bytes, fetcher


def _rotated_repository(target_bytes: bytes) -> tuple[bytes, _RepositoryFetcher]:
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
    root2_metadata.sign(old_root)
    root2_metadata.sign(new_root, append=True)

    _, fetcher = _signed_repository(
        target_bytes,
        version=2,
        signers={"root": new_root, **signers},
        root_bytes=root1_bytes,
    )
    fetcher.files["2.root.json"] = root2_metadata.to_bytes()
    return root1_bytes, fetcher


def _trust(
    tmp_path: Path, root_bytes: bytes, fetcher: _RepositoryFetcher
) -> UpdateTrust:
    return UpdateTrust(
        metadata_root=tmp_path / "metadata",
        target_root=tmp_path / "targets",
        metadata_base_url="https://updates.example.test/platform/metadata/",
        target_base_url="https://updates.example.test/platform/targets/",
        bootstrap_root=root_bytes,
        fetcher=fetcher,
    )


def test_update_trust_refreshes_and_returns_verified_target_bytes(tmp_path: Path) -> None:
    target_bytes = (json.dumps(_manifest(), sort_keys=True) + "\n").encode()
    root_bytes, fetcher = _signed_repository(target_bytes)
    trust = _trust(tmp_path, root_bytes, fetcher)

    trust.refresh()
    target = trust.trusted_target("platform-release.json")

    assert target.data == target_bytes
    assert target.length == len(target_bytes)
    assert target.sha256 == __import__("hashlib").sha256(target_bytes).hexdigest()
    state = json.loads((tmp_path / "metadata/trusted-state.json").read_text())
    assert state == {"root": 1, "snapshot": 1, "targets": 1, "timestamp": 1}


def test_update_trust_accepts_valid_root_rotation_and_persists_new_floor(
    tmp_path: Path,
) -> None:
    target_bytes = (json.dumps(_manifest(), sort_keys=True) + "\n").encode()
    root_bytes, fetcher = _rotated_repository(target_bytes)
    trust = _trust(tmp_path, root_bytes, fetcher)

    trust.refresh()

    state = json.loads((tmp_path / "metadata/trusted-state.json").read_text())
    assert state["root"] == 2
    assert "https://updates.example.test/platform/metadata/2.root.json" in fetcher.urls


@pytest.mark.parametrize("failure", ["expired", "target-bytes", "snapshot-bytes"])
def test_update_trust_rejects_expiry_and_metadata_or_target_mismatch(
    tmp_path: Path, failure: str
) -> None:
    target_bytes = (json.dumps(_manifest(), sort_keys=True) + "\n").encode()
    root_bytes, fetcher = _signed_repository(
        target_bytes, expired=failure == "expired"
    )
    if failure == "target-bytes":
        fetcher.files["platform-release.json"] = b"tampered"
    if failure == "snapshot-bytes":
        fetcher.files["snapshot.json"] = b"tampered"
    trust = _trust(tmp_path, root_bytes, fetcher)

    with pytest.raises(UpdateTrustError):
        trust.refresh()
        trust.trusted_target("platform-release.json")


def test_update_trust_rejects_metadata_version_rollback(tmp_path: Path) -> None:
    target_bytes = (json.dumps(_manifest(), sort_keys=True) + "\n").encode()
    signers = {
        role: CryptoSigner.generate_ed25519()
        for role in ("root", "timestamp", "snapshot", "targets")
    }
    root_bytes, newer = _signed_repository(target_bytes, version=2, signers=signers)
    trust = _trust(tmp_path, root_bytes, newer)
    trust.refresh()

    _, older = _signed_repository(
        target_bytes, version=1, signers=signers, root_bytes=root_bytes
    )
    replay = _trust(tmp_path, root_bytes, older)

    with pytest.raises(UpdateTrustError):
        replay.refresh()


def test_update_trust_rejects_symlinked_cache_without_mutating_target(
    tmp_path: Path,
) -> None:
    target_bytes = (json.dumps(_manifest(), sort_keys=True) + "\n").encode()
    root_bytes, fetcher = _signed_repository(target_bytes)
    redirected = tmp_path / "redirected"
    redirected.mkdir(mode=0o755)
    (tmp_path / "metadata").symlink_to(redirected, target_is_directory=True)
    trust = _trust(tmp_path, root_bytes, fetcher)

    with pytest.raises(UpdateTrustError, match="cache directory"):
        trust.refresh()

    assert redirected.stat().st_mode & 0o777 == 0o755
    assert list(redirected.iterdir()) == []


def test_update_trust_recovers_stale_atomic_state_temporary(tmp_path: Path) -> None:
    target_bytes = (json.dumps(_manifest(), sort_keys=True) + "\n").encode()
    root_bytes, fetcher = _signed_repository(target_bytes)
    trust = _trust(tmp_path, root_bytes, fetcher)
    trust.refresh()
    stale = tmp_path / "metadata/.trusted-state.json.new"
    stale.write_text("interrupted", encoding="utf-8")

    trust.refresh()

    assert json.loads((tmp_path / "metadata/trusted-state.json").read_text())["root"] == 1
