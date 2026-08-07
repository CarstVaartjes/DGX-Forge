from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, tzinfo
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from vonk_control import update_grants
from vonk_control.update_authority import UpdateAuthorizationError
from vonk_control.update_grants import AdminActionGrantError, AdminActionGrantIssuer
from vonk_control.update_signer import AdminActionGrantVerifier

NOW = datetime.fromtimestamp(1_800_000_000, tz=UTC)
ROLLOUT = "11111111-1111-4111-8111-111111111111"
PARENT = "22222222-2222-4222-8222-222222222222"
NONCE = "33333333-3333-4333-8333-333333333333"
NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
TARGET = "sha256:" + "c" * 64


class _UndefinedOffset(tzinfo):
    def utcoffset(self, value):
        return None

    def dst(self, value):
        return None


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("ascii")


def _private_key(path: Path) -> ed25519.Ed25519PrivateKey:
    key = ed25519.Ed25519PrivateKey.generate()
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    return key


def _issuer(tmp_path: Path) -> tuple[AdminActionGrantIssuer, ed25519.Ed25519PrivateKey]:
    key_path = tmp_path / "admin-grant.pem"
    key = _private_key(key_path)
    return (
        AdminActionGrantIssuer.from_private_key_file(
            key_path,
            clock=lambda: NOW,
            nonce_factory=lambda: uuid.UUID(NONCE),
        ),
        key,
    )


def test_update_grant_is_canonical_deterministic_and_signer_compatible(
    tmp_path: Path,
) -> None:
    issuer, key = _issuer(tmp_path)

    grant = issuer.issue(
        action="agent.update",
        rollout_id=ROLLOUT,
        parent_job_id=PARENT,
        node_ids=[NODE_B, NODE_A],
        target_release_digest=TARGET,
        expires_at=1_800_003_600,
    )

    claims = {
        "action": "agent.update",
        "expires_at": 1_800_003_600,
        "nonce": NONCE,
        "node_ids": [NODE_A, NODE_B],
        "parent_job_id": PARENT,
        "rollout_id": ROLLOUT,
        "schema_version": 1,
        "target_release_digest": TARGET,
    }
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    assert grant == {
        "claims": claims,
        "signature": {
            "algorithm": "ed25519",
            "key_id": hashlib.sha256(public).hexdigest(),
            "value": key.sign(_canonical(claims)).hex(),
        },
    }
    verifier = AdminActionGrantVerifier(
        key.public_key(),
        key_id=hashlib.sha256(public).hexdigest(),
        clock=lambda: NOW,
    )
    verifier.verify(
        {
            "action": "agent.update",
            "admin_grant": grant,
            "expires_at": 1_800_003_000,
            "node_id": NODE_B,
            "parent_job_id": PARENT,
            "rollout_id": ROLLOUT,
            "target_release_digest": TARGET,
        }
    )
    assert issuer.public_key_bytes() == _canonical(
        {
            "algorithm": "ed25519",
            "key_id": hashlib.sha256(public).hexdigest(),
            "public_key": public.hex(),
            "schema_version": 1,
        }
    )


def test_rollback_grant_requires_and_signs_null_target(tmp_path: Path) -> None:
    issuer, key = _issuer(tmp_path)

    grant = issuer.issue(
        action="agent.rollback",
        rollout_id=ROLLOUT,
        parent_job_id=PARENT,
        node_ids=(NODE_A,),
        target_release_digest=None,
        expires_at=1_800_000_001,
    )

    AdminActionGrantVerifier(
        key.public_key(), key_id=issuer.key_id, clock=lambda: NOW
    ).verify(
        {
            "action": "agent.rollback",
            "admin_grant": grant,
            "expires_at": 1_800_000_001,
            "node_id": NODE_A,
            "parent_job_id": PARENT,
            "rollout_id": ROLLOUT,
            "target_release_digest": None,
        }
    )
    assert grant["claims"]["target_release_digest"] is None


@pytest.mark.parametrize(
    ("change", "value"),
    (
        ("action", "agent.install"),
        ("action", []),
        ("rollout_id", "11111111-1111-1111-8111-111111111111"),
        ("parent_job_id", "ABCDEFAB-CDEF-4ABC-8DEF-ABCDEFABCDEF"),
        ("node_ids", []),
        ("node_ids", [NODE_A, NODE_A]),
        ("node_ids", ["node-a"]),
        ("node_ids", "not-a-sequence-of-node-ids"),
        ("expires_at", True),
        ("expires_at", 1_800_000_000),
        ("expires_at", 1_800_003_601),
        ("target_release_digest", None),
        ("target_release_digest", "c" * 64),
    ),
)
def test_update_grant_rejects_invalid_or_unbounded_claims(
    tmp_path: Path, change: str, value: object
) -> None:
    issuer, _key = _issuer(tmp_path)
    arguments: dict[str, object] = {
        "action": "agent.update",
        "rollout_id": ROLLOUT,
        "parent_job_id": PARENT,
        "node_ids": [NODE_A],
        "target_release_digest": TARGET,
        "expires_at": 1_800_000_001,
    }
    arguments[change] = value

    with pytest.raises(AdminActionGrantError):
        issuer.issue(**arguments)


def test_grant_rejects_excessive_node_count_and_invalid_generated_nonce(
    tmp_path: Path,
) -> None:
    issuer, _key = _issuer(tmp_path)
    nodes = [f"spk_{index:032x}" for index in range(1025)]
    with pytest.raises(AdminActionGrantError):
        issuer.issue(
            action="agent.update",
            rollout_id=ROLLOUT,
            parent_job_id=PARENT,
            node_ids=nodes,
            target_release_digest=TARGET,
            expires_at=1_800_000_001,
        )

    broken = AdminActionGrantIssuer(
        ed25519.Ed25519PrivateKey.generate(),
        clock=lambda: NOW,
        nonce_factory=lambda: uuid.uuid1(),
    )
    with pytest.raises(AdminActionGrantError):
        broken.issue(
            action="agent.rollback",
            rollout_id=ROLLOUT,
            parent_job_id=PARENT,
            node_ids=[NODE_A],
            target_release_digest=None,
            expires_at=1_800_000_001,
        )


def test_private_key_loader_uses_identity_stable_owner_only_snapshot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "admin-grant.pem"
    original = _private_key(path)
    issuer = AdminActionGrantIssuer.from_private_key_file(
        path, clock=lambda: NOW, nonce_factory=lambda: uuid.UUID(NONCE)
    )
    replacement = ed25519.Ed25519PrivateKey.generate()
    path.write_bytes(
        replacement.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    grant = issuer.issue(
        action="agent.rollback",
        rollout_id=ROLLOUT,
        parent_job_id=PARENT,
        node_ids=[NODE_A],
        target_release_digest=None,
        expires_at=1_800_000_001,
    )

    original.public_key().verify(
        bytes.fromhex(grant["signature"]["value"]),
        _canonical(grant["claims"]),
    )


def test_private_key_loader_rejects_file_not_owned_by_service_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "admin-grant.pem"
    _private_key(path)
    owner = os.geteuid()
    monkeypatch.setattr(update_grants.os, "geteuid", lambda: owner + 1)

    with pytest.raises(AdminActionGrantError):
        AdminActionGrantIssuer.from_private_key_file(path)


def test_issuer_and_signer_reject_timezone_with_undefined_offset(
    tmp_path: Path,
) -> None:
    key = ed25519.Ed25519PrivateKey.generate()
    undefined = datetime(2027, 1, 1, tzinfo=_UndefinedOffset())
    undefined_expiry = int(undefined.astimezone(UTC).timestamp()) + 1
    issuer = AdminActionGrantIssuer(
        key,
        clock=lambda: undefined,
        nonce_factory=lambda: uuid.UUID(NONCE),
    )
    with pytest.raises(AdminActionGrantError):
        issuer.issue(
            action="agent.rollback",
            rollout_id=ROLLOUT,
            parent_job_id=PARENT,
            node_ids=[NODE_A],
            target_release_digest=None,
            expires_at=undefined_expiry,
        )

    valid, _key = _issuer(tmp_path)
    grant = valid.issue(
        action="agent.rollback",
        rollout_id=ROLLOUT,
        parent_job_id=PARENT,
        node_ids=[NODE_A],
        target_release_digest=None,
        expires_at=1_800_000_001,
    )
    with pytest.raises(UpdateAuthorizationError):
        AdminActionGrantVerifier(
            _key.public_key(), key_id=valid.key_id, clock=lambda: undefined
        ).verify(
            {
                "action": "agent.rollback",
                "admin_grant": grant,
                "expires_at": undefined_expiry,
                "node_id": NODE_A,
                "parent_job_id": PARENT,
                "rollout_id": ROLLOUT,
                "target_release_digest": None,
            }
        )


@pytest.mark.parametrize("fault", ("symlink", "hardlink", "permissions", "wrong-key"))
def test_private_key_loader_rejects_unsafe_or_non_ed25519_key(
    tmp_path: Path, fault: str
) -> None:
    path = tmp_path / "admin-grant.pem"
    _private_key(path)
    if fault == "symlink":
        target = tmp_path / "target.pem"
        path.rename(target)
        path.symlink_to(target)
    elif fault == "hardlink":
        os.link(path, tmp_path / "second-link.pem")
    elif fault == "permissions":
        path.chmod(0o640)
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )

    with pytest.raises(AdminActionGrantError):
        AdminActionGrantIssuer.from_private_key_file(path)


def test_issued_grant_rejects_tampering_in_signer_verifier(tmp_path: Path) -> None:
    issuer, key = _issuer(tmp_path)
    grant = issuer.issue(
        action="agent.update",
        rollout_id=ROLLOUT,
        parent_job_id=PARENT,
        node_ids=[NODE_A],
        target_release_digest=TARGET,
        expires_at=1_800_000_001,
    )
    grant["claims"]["node_ids"] = [NODE_B]

    with pytest.raises(UpdateAuthorizationError):
        AdminActionGrantVerifier(
            key.public_key(), key_id=issuer.key_id, clock=lambda: NOW
        ).verify(
            {
                "action": "agent.update",
                "admin_grant": grant,
                "expires_at": 1_800_000_001,
                "node_id": NODE_B,
                "parent_job_id": PARENT,
                "rollout_id": ROLLOUT,
                "target_release_digest": TARGET,
            }
        )
