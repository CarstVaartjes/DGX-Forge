from __future__ import annotations

import os
from pathlib import Path

import pytest
from dgx_control.host_state import HostOperationPlan, PhaseJournal
from dgx_control.upgrade import (
    PhaseDispatcher,
    PhaseObservation,
    PhaseStep,
    ProbeDisposition,
    UpgradePhase,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _plan() -> HostOperationPlan:
    return HostOperationPlan(
        operation_id="operation-1",
        plan_digest=f"sha256:{SHA_A}",
        generation_id="gen-" + SHA_B[:24],
        platform_target_name=f"platform/releases/1.2.0/{SHA_B}.json",
        platform_target_sha256=SHA_B,
        tuf_targets_version=7,
        release_digest=f"sha256:{SHA_B}",
        build_digest=f"sha256:{SHA_C}",
        platform_version="1.2.0",
        deployment_bundle_digest=f"sha256:{SHA_D}",
        api_image=f"ghcr.io/example/api@sha256:{SHA_A}",
        worker_image=f"ghcr.io/example/worker@sha256:{SHA_B}",
        database_revision="0012_control_process_heartbeats",
    )


class InjectedCrash(RuntimeError):
    pass


def test_dispatcher_adopts_exact_effect_after_crash_before_journal_append(
    tmp_path: Path,
) -> None:
    """Removing the post-perform probe would duplicate a completed side effect."""

    effect = {"complete": False, "perform_count": 0, "crash": True}

    def probe() -> PhaseObservation:
        if effect["complete"]:
            return PhaseObservation(
                ProbeDisposition.EXACT,
                {"target_sha256": SHA_B},
            )
        return PhaseObservation(ProbeDisposition.ABSENT, {})

    def perform() -> None:
        effect["perform_count"] += 1
        effect["complete"] = True
        if effect["crash"]:
            raise InjectedCrash("after effect, before journal append")

    plan = _plan()
    journal = PhaseJournal(
        tmp_path / "control-host",
        operation_id=plan.operation_id,
        owner_uid=os.geteuid(),
    )
    state = journal.create(plan)
    step = PhaseStep(UpgradePhase.AUTHORIZED, probe, perform)

    with pytest.raises(InjectedCrash):
        PhaseDispatcher(journal).run(state, (step,))

    assert (
        PhaseJournal(tmp_path / "control-host", owner_uid=os.geteuid())
        .load_pending()
        .entries
        == ()
    )
    effect["crash"] = False
    recovered = PhaseDispatcher(journal).run(
        PhaseJournal(tmp_path / "control-host", owner_uid=os.geteuid()).load_pending(),
        (step,),
    )

    assert effect["perform_count"] == 1
    assert [entry.phase for entry in recovered.entries] == ["authorized"]
    assert recovered.entries[0].evidence == {"target_sha256": SHA_B}


def test_dispatcher_rejects_a_journal_that_is_not_an_exact_program_prefix(
    tmp_path: Path,
) -> None:
    """Accepting a reordered phase would make crash recovery skip authorization."""

    plan = _plan()
    journal = PhaseJournal(
        tmp_path / "control-host",
        operation_id=plan.operation_id,
        owner_uid=os.geteuid(),
    )
    journal.create(plan)
    state = journal.append("generation-staged", {"generation_id": "gen-a"})
    step = PhaseStep(
        UpgradePhase.AUTHORIZED,
        lambda: PhaseObservation(ProbeDisposition.EXACT, {}),
        lambda: None,
    )

    with pytest.raises(Exception, match="phase order"):
        PhaseDispatcher(journal).run(state, (step,))


def test_dispatcher_revalidates_recorded_authorization_before_recovery(
    tmp_path: Path,
) -> None:
    """Skipping a recorded authorization probe would ignore a later revocation."""

    plan = _plan()
    journal = PhaseJournal(
        tmp_path / "control-host",
        operation_id=plan.operation_id,
        owner_uid=os.geteuid(),
    )
    journal.create(plan)
    state = journal.append("authorized", {"target_sha256": SHA_B})
    step = PhaseStep(
        UpgradePhase.AUTHORIZED,
        lambda: PhaseObservation(ProbeDisposition.CONFLICT, {}),
        lambda: None,
        recheck_on_resume=True,
    )

    with pytest.raises(Exception, match="authorized exact probe conflicted"):
        PhaseDispatcher(journal).run(state, (step,))
