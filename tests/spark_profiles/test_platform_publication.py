from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from spark_profiles.platform_publication import (
    LocalPlatformPublicationStore,
    PlatformPublicationError,
)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _target(version: str, marker: str) -> tuple[str, bytes, str]:
    raw = _canonical({"marker": marker, "platform_version": version})
    digest = hashlib.sha256(raw).hexdigest()
    return f"platform/releases/{version}/{digest}.json", raw, digest


def _metadata(version: int, retained: list[str]) -> dict[str, object]:
    return {
        "retained_targets": retained,
        "targets_version": version,
    }


def _bundle(marker: str) -> dict[str, object]:
    return {
        "manifest_digest": f"sha256:{marker * 64}",
        "reference": f"ghcr.io/example/control@sha256:{marker * 64}",
    }


def test_local_target_publication_is_content_addressed_and_idempotent(
    tmp_path: Path,
) -> None:
    store = LocalPlatformPublicationStore(tmp_path / "repository")
    target_name, raw, digest = _target("1.2.0", "n")
    predecessor, predecessor_raw, _ = _target("1.1.0", "p")
    store.publish_target(
        predecessor,
        predecessor_raw,
        _bundle("a"),
        _metadata(18, []),
    )

    first = store.publish_target(
        target_name,
        raw,
        _bundle("b"),
        _metadata(19, [predecessor]),
    )
    replay = store.publish_target(
        target_name,
        raw,
        _bundle("b"),
        _metadata(19, [predecessor]),
    )

    assert replay == first
    assert first["target_name"] == target_name
    assert first["target_sha256"] == digest
    assert first["targets_version"] == 19
    assert store.read_target(target_name) == raw
    assert store.read_target(predecessor) == predecessor_raw


def test_local_target_publication_rejects_alias_overwrite_and_missing_retention(
    tmp_path: Path,
) -> None:
    store = LocalPlatformPublicationStore(tmp_path / "repository")
    target_name, raw, _ = _target("1.2.0", "n")
    predecessor, _, _ = _target("1.1.0", "p")

    with pytest.raises(PlatformPublicationError, match="immutable target"):
        store.publish_target(
            "platform-release.json", raw, _bundle("a"), _metadata(1, [])
        )
    with pytest.raises(PlatformPublicationError, match="retained"):
        store.publish_target(
            target_name, raw, _bundle("a"), _metadata(2, [predecessor])
        )

    store.publish_target(target_name, raw, _bundle("a"), _metadata(2, []))
    with pytest.raises(PlatformPublicationError, match="immutable"):
        store.publish_target(
            target_name,
            raw,
            _bundle("b"),
            _metadata(2, []),
        )


def test_channel_publication_is_monotonic_cas_with_exact_replay(
    tmp_path: Path,
) -> None:
    store = LocalPlatformPublicationStore(tmp_path / "repository")
    target_n, raw_n, sha_n = _target("1.2.0", "n")
    target_next, raw_next, sha_next = _target("1.3.0", "x")
    target_other, raw_other, sha_other = _target("1.3.1", "y")
    store.publish_target(target_n, raw_n, _bundle("a"), _metadata(19, []))
    store.publish_target(target_next, raw_next, _bundle("b"), _metadata(20, []))
    store.publish_target(target_other, raw_other, _bundle("c"), _metadata(20, []))
    channel_n = _canonical(
        {
            "channel": "stable",
            "discovery_only": True,
            "schema_version": 1,
            "target_name": target_n,
            "target_sha256": sha_n,
            "tuf_targets_version": 19,
        }
    )
    channel_next = _canonical(
        {
            "channel": "stable",
            "discovery_only": True,
            "schema_version": 1,
            "target_name": target_next,
            "target_sha256": sha_next,
            "tuf_targets_version": 20,
        }
    )

    first = store.publish_channel("stable", channel_n)
    assert store.publish_channel("stable", channel_n) == first
    assert store.publish_channel("stable", channel_next)["target_name"] == target_next

    with pytest.raises(PlatformPublicationError, match="monotonic"):
        store.publish_channel("stable", channel_n)
    equal_version_other = _canonical(
        json.loads(channel_next)
        | {"target_name": target_other, "target_sha256": sha_other}
    )
    with pytest.raises(PlatformPublicationError, match="monotonic"):
        store.publish_channel("stable", equal_version_other)


def test_channel_rejects_unpublished_target_and_noncanonical_bytes(
    tmp_path: Path,
) -> None:
    store = LocalPlatformPublicationStore(tmp_path / "repository")
    target_name, _, digest = _target("1.2.0", "n")
    document = {
        "channel": "stable",
        "discovery_only": True,
        "schema_version": 1,
        "target_name": target_name,
        "target_sha256": digest,
        "tuf_targets_version": 19,
    }

    with pytest.raises(PlatformPublicationError, match="published target"):
        store.publish_channel("stable", _canonical(document))
    with pytest.raises(PlatformPublicationError, match="canonical"):
        store.publish_channel("stable", json.dumps(document, indent=2).encode())
