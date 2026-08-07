from __future__ import annotations

import json
from pathlib import Path

import pytest
from vonk_agent.package_helper import ActiveSlotVerifier, PackageHelper
from vonk_agent.package_helper_protocol import HelperProtocolError
from vonk_agent.packages.sandbox import SandboxPolicy

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def state_document(digest: str = DIGEST_A) -> dict[str, object]:
    return {
        "activation_deadline": None,
        "active_slot": "A",
        "boot_attempts": 0,
        "expected_sha256": digest,
        "generation": 1,
        "previous_slot": None,
        "rollback_performed": False,
        "schema_version": 1,
        "slot_sha256": {"A": digest, "B": None},
        "status": "stable",
    }


def write_state(path: Path, value: dict[str, object]) -> None:
    path.write_bytes(
        (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    path.chmod(0o644)


def test_active_slot_verifier_accepts_only_the_current_expected_helper_slot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    write_state(path, state_document())
    verifier = ActiveSlotVerifier(
        DIGEST_A, path, allow_unprivileged_test_file=True
    )

    verifier.verify()

    write_state(path, state_document(DIGEST_B))
    with pytest.raises(HelperProtocolError, match="active slot"):
        verifier.verify()


def test_active_slot_verifier_rejects_noncanonical_or_unsafe_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state_document(), indent=2))
    path.chmod(0o644)
    verifier = ActiveSlotVerifier(
        DIGEST_A, path, allow_unprivileged_test_file=True
    )

    with pytest.raises(HelperProtocolError, match="state"):
        verifier.verify()

    write_state(path, state_document())
    path.chmod(0o666)
    with pytest.raises(HelperProtocolError, match="unsafe"):
        verifier.verify()


def test_active_slot_verifier_fail_closes_nonfinite_json(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    document = state_document()
    document["activation_deadline"] = float("nan")
    write_state(path, document)
    verifier = ActiveSlotVerifier(
        DIGEST_A, path, allow_unprivileged_test_file=True
    )

    with pytest.raises(HelperProtocolError, match="state"):
        verifier.verify()


def test_active_slot_verifier_rejects_symlink_state(tmp_path: Path) -> None:
    target = tmp_path / "real.json"
    write_state(target, state_document())
    link = tmp_path / "state.json"
    link.symlink_to(target)
    verifier = ActiveSlotVerifier(
        DIGEST_A, link, allow_unprivileged_test_file=True
    )

    with pytest.raises(HelperProtocolError, match="unavailable"):
        verifier.verify()


def test_package_helper_revalidates_active_slot_before_parsing_every_request() -> None:
    class RejectingSlot:
        calls = 0

        def verify(self) -> None:
            self.calls += 1
            raise HelperProtocolError("active slot changed")

    class Boundary:
        def verify(self, receipt):
            return True

        def authorize(self, request, request_digest):
            return True

        def launch(self, request, sandbox):
            raise AssertionError("stale helper must not launch")

    slot = RejectingSlot()
    boundary = Boundary()
    helper = PackageHelper(
        agent_uid=64000,
        sandbox=SandboxPolicy(64001, 64001, allowed_devices=("nvidia0",)),
        active_slot_verifier=slot,
        receipt_verifier=boundary,
        fence_authorizer=boundary,
        launcher=boundary,
    )

    with pytest.raises(HelperProtocolError, match="active slot"):
        helper.handle(64000, b"not-json")
    assert slot.calls == 1
