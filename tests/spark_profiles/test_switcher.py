from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from spark_profiles.admission import AdmissionReport, check_admission
from spark_profiles.backend import CommandResult
from spark_profiles.catalog import Catalog, fingerprint
from spark_profiles.contracts import (
    AdapterCommands,
    CheckpointPin,
    ClusterProfile,
    Endpoint,
    ImagePin,
    ResourceEnvelope,
    SourcePin,
    WorkloadDefinition,
    WorkloadPaths,
)
from spark_profiles.state import ControllerState, StateStore
from spark_profiles.switcher import ProfileSwitcher

SHA_A = "a" * 64
BOOT_IDS = {"spark1": "1" * 32, "spark2": "2" * 32}
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def command_result(ok: bool = True, stderr: bytes = b"") -> CommandResult:
    return CommandResult(
        returncode=0 if ok else 1,
        stdout=b"",
        stderr=stderr,
        timed_out=False,
        stdout_truncated=False,
        stderr_truncated=False,
    )


def workload(
    identifier: str = "generator", *, distributed: bool = False
) -> WorkloadDefinition:
    nodes = ("spark1", "spark2") if distributed else ("spark2",)
    start_order = ("spark2", "spark1") if distributed else ("spark2",)
    stop_order = ("spark1", "spark2") if distributed else ("spark2",)
    command = lambda operation: (f"profile-{operation}", identifier)
    return WorkloadDefinition(
        id=identifier,
        adapter="fixture",
        topology="distributed" if distributed else "single",
        placement_class="dual-exclusive" if distributed else "single-exclusive",
        nodes=nodes,
        start_order=start_order,
        stop_order=stop_order,
        conflicts=(),
        co_location="exclusive",
        accepted_evidence=Path("accepted.json"),
        source=SourcePin("https://example.test/source", "1" * 40),
        checkpoint=CheckpointPin(
            "example/checkpoint", "2" * 40, Path("/srv/manifest.json"), "3" * 64
        ),
        image=ImagePin("example.test/image@sha256:" + "4" * 64),
        paths=WorkloadPaths(
            Path(f"/srv/cache/{identifier}"),
            Path(f"/srv/scratch/{identifier}"),
            Path(f"/srv/output/{identifier}"),
        ),
        endpoint=Endpoint("127.0.0.1", 9000),
        commands=AdapterCommands(
            prepare=command("prepare"),
            verify=command("verify"),
            start=command("start"),
            health=command("health"),
            infer=command("infer"),
            stop=command("stop"),
            verify_release=command("verify-release"),
        ),
        resources=ResourceEnvelope(1, 1, 1),
    )


def profile(identifier: str, definition: WorkloadDefinition | None) -> ClusterProfile:
    placements = {"spark1": (), "spark2": ()}
    endpoints = {}
    if definition is not None:
        for node in definition.nodes:
            placements[node] = (definition.id,)
        endpoints = {"model": definition.id}
    return ClusterProfile(
        id=identifier,
        accepted_evidence=Path("accepted.json"),
        placements=placements,
        endpoints=endpoints,
    )


def catalog(*profiles: ClusterProfile, definition: WorkloadDefinition) -> Catalog:
    definition_sha = fingerprint(definition)
    profile_map = {item.id: item for item in profiles}
    accepted_profiles = {
        fingerprint(item): (definition_sha,)
        for item in profiles
        if any(item.placements.values())
    }
    return Catalog(
        definitions={definition.id: definition},
        profiles=profile_map,
        selectors={"default": profiles[0].id},
        definition_fingerprints={definition.id: definition_sha},
        profile_fingerprints={
            key: fingerprint(value) for key, value in profile_map.items()
        },
        maturity={definition.id: "accepted"},
        maturity_fingerprints={definition.id: definition_sha},
        accepted_profiles=accepted_profiles,
    )


class FakeBackend:
    def __init__(
        self, events: list[tuple], *, fail: tuple[str, str] | None = None
    ) -> None:
        self.events = events
        self.fail = fail

    def run(self, node: str, argv: tuple[str, ...], timeout: float) -> CommandResult:
        self.events.append(("remote", node, argv))
        operation = argv[0]
        identifier = argv[1]
        if self.fail == (operation, identifier):
            return command_result(False, b"fixture failure")
        return command_result()


class FakeStore:
    def __init__(self, state: ControllerState, events: list[tuple]) -> None:
        self.state = state
        self.events = events
        self.saves: list[ControllerState] = []

    @contextmanager
    def acquire(self):
        self.events.append(("lock",))
        yield self.state

    def save(self, state: ControllerState) -> None:
        self.state = state
        self.saves.append(state)
        self.events.append(("save", state.status, state.active_profile))

    def load(self) -> ControllerState:
        return self.state


class FailingSaveStore(FakeStore):
    def __init__(
        self,
        state: ControllerState,
        events: list[tuple],
        *,
        fail_from_attempt: int,
        fail_once: bool = False,
    ) -> None:
        super().__init__(state, events)
        self.fail_from_attempt = fail_from_attempt
        self.fail_once = fail_once
        self.save_attempts = 0

    def save(self, state: ControllerState) -> None:
        self.save_attempts += 1
        should_fail = self.save_attempts >= self.fail_from_attempt and (
            not self.fail_once or self.save_attempts == self.fail_from_attempt
        )
        if should_fail:
            self.events.append(("save-error", state.status, state.active_profile))
            raise OSError(f"fixture save failure {self.save_attempts}")
        super().save(state)


def inventory(
    *, boot_ids: dict[str, str] | None = None
) -> dict[str, dict[str, int | bool | str]]:
    live_boot_ids = boot_ids or BOOT_IDS
    return {
        "spark1": {
            "healthy": True,
            "free_memory_bytes": 100,
            "free_disk_bytes": 100,
            "boot_id": live_boot_ids["spark1"],
        },
        "spark2": {
            "healthy": True,
            "free_memory_bytes": 100,
            "free_disk_bytes": 100,
            "boot_id": live_boot_ids["spark2"],
        },
    }


def make_switcher(
    catalog_value: Catalog,
    state: ControllerState,
    *,
    fail: tuple[str, str] | None = None,
) -> tuple[ProfileSwitcher, list[tuple], FakeStore]:
    events: list[tuple] = []
    store = FakeStore(state, events)
    switcher = ProfileSwitcher(
        catalog=catalog_value,
        backend=FakeBackend(events, fail=fail),
        state_store=store,
        inventory_provider=inventory,
        timeout_seconds=10,
    )
    return switcher, events, store


def active_state(
    profile_value: ClusterProfile, definition: WorkloadDefinition
) -> ControllerState:
    return ControllerState(
        status="active",
        active_profile=profile_value.id,
        target_profile=None,
        restore_profile=None,
        last_error=None,
        active_profile_sha256=fingerprint(profile_value),
        active_definition_sha256={definition.id: fingerprint(definition)},
        boot_ids=BOOT_IDS,
    )


def test_distributed_stop_is_head_first_and_start_is_worker_first() -> None:
    definition = workload("deepseek-agent-dual", distributed=True)
    current = profile("current", definition)
    maintenance = profile("maintenance", None)
    target = replace(current, id="target")
    catalog_value = catalog(current, maintenance, target, definition=definition)
    switcher, events, _ = make_switcher(
        catalog_value, active_state(current, definition)
    )

    stopped = switcher.switch_profile("maintenance")
    assert stopped.status == "stopped"
    assert [event for event in events if event[0] == "remote"][:2] == [
        ("remote", "spark1", ("profile-stop", "deepseek-agent-dual", "head")),
        ("remote", "spark2", ("profile-stop", "deepseek-agent-dual", "worker")),
    ]

    events.clear()
    started = switcher.switch_profile("target")
    assert started.status == "active"
    start_calls = [
        event
        for event in events
        if event[0] == "remote" and event[2][0] == "profile-start"
    ]
    assert start_calls == [
        ("remote", "spark2", ("profile-start", "deepseek-agent-dual", "worker")),
        ("remote", "spark1", ("profile-start", "deepseek-agent-dual", "head")),
    ]


def test_dry_run_does_not_call_backend_or_save_state() -> None:
    definition = workload()
    target = profile("target", definition)
    catalog_value = catalog(target, definition=definition)
    switcher, events, store = make_switcher(catalog_value, ControllerState.stopped())

    report = switcher.switch_profile("target", dry_run=True)

    assert report.status == "planned"
    assert events == []
    assert store.saves == []


def test_dry_run_with_real_store_creates_no_state_directory_or_lock(
    tmp_path: Path,
) -> None:
    definition = workload()
    target = profile("target", definition)
    catalog_value = catalog(target, definition=definition)
    state_directory = tmp_path / "state"
    events: list[tuple] = []
    switcher = ProfileSwitcher(
        catalog=catalog_value,
        backend=FakeBackend(events),
        state_store=StateStore(state_directory),
        inventory_provider=inventory,
    )

    report = switcher.switch_profile("target", dry_run=True)

    assert report.status == "planned"
    assert report.dry_run is True
    assert events == []
    assert not state_directory.exists()


def test_planned_definition_is_not_activated() -> None:
    definition = workload()
    target = profile("target", definition)
    catalog_value = catalog(target, definition=definition)
    catalog_value.maturity[definition.id] = "planned"
    switcher, events, store = make_switcher(catalog_value, ControllerState.stopped())

    report = switcher.switch_profile("target")

    assert report.status == "blocked"
    assert "maturity is planned" in " ".join(report.errors)
    assert [event for event in events if event[0] == "remote"] == []
    assert store.saves == []


def test_profile_without_exact_accepted_evidence_is_not_activated() -> None:
    definition = workload()
    target = profile("target", definition)
    catalog_value = catalog(target, definition=definition)
    catalog_value.accepted_profiles = {}
    switcher, events, store = make_switcher(
        catalog_value, ControllerState.stopped()
    )

    report = switcher.switch_profile("target")

    assert report.status == "blocked"
    assert report.errors == ("profile has no exact accepted evidence",)
    assert [event for event in events if event[0] == "remote"] == []
    assert store.saves == []


def test_checked_in_production_home_remains_truthfully_unactivatable() -> None:
    catalog_value = Catalog.load(REPOSITORY_ROOT)
    switcher, events, store = make_switcher(catalog_value, ControllerState.stopped())

    report = switcher.switch_profile("default")

    assert report.target_profile == "agent-full-dual"
    assert report.status == "blocked"
    assert "deepseek-agent-dual maturity is planned" in report.errors
    assert [event for event in events if event[0] == "remote"] == []
    assert store.saves == []


def test_changed_endpoint_is_withdrawn_before_stop() -> None:
    definition = workload()
    current = profile("current", definition)
    maintenance = profile("maintenance", None)
    catalog_value = catalog(current, maintenance, definition=definition)
    switcher, events, _ = make_switcher(
        catalog_value, active_state(current, definition)
    )

    switcher.switch_profile("maintenance")

    transitioning = events.index(("save", "transitioning", None))
    first_stop = next(
        index
        for index, event in enumerate(events)
        if event[0] == "remote" and event[2][0] == "profile-stop"
    )
    assert transitioning < first_stop


def test_retention_requires_matching_hashes_and_health() -> None:
    definition = workload()
    current = profile("current", definition)
    target = replace(current, id="target")
    catalog_value = catalog(current, target, definition=definition)
    switcher, events, _ = make_switcher(
        catalog_value, active_state(current, definition)
    )

    report = switcher.switch_profile("target")

    assert report.retained_workloads == (definition.id,)
    operations = [event[2][0] for event in events if event[0] == "remote"]
    assert operations == ["profile-health", "profile-health", "profile-infer"]

    stale = replace(
        active_state(current, definition),
        active_definition_sha256={definition.id: SHA_A},
    )
    switcher, events, _ = make_switcher(catalog_value, stale)
    stale_report = switcher.switch_profile("target")
    operations = [event[2][0] for event in events if event[0] == "remote"]
    assert stale_report.status == "blocked"
    assert operations == []


def test_successful_activation_captures_exact_live_boot_ids() -> None:
    definition = workload()
    target = profile("target", definition)
    catalog_value = catalog(target, definition=definition)
    switcher, _, store = make_switcher(catalog_value, ControllerState.stopped())

    report = switcher.switch_profile("target")

    assert report.status == "active"
    assert dict(store.state.boot_ids) == BOOT_IDS


def test_boot_id_change_forces_restart_instead_of_retention() -> None:
    definition = workload()
    current = profile("current", definition)
    target = replace(current, id="target")
    catalog_value = catalog(current, target, definition=definition)
    events: list[tuple] = []
    store = FakeStore(active_state(current, definition), events)
    new_boot_ids = {"spark1": BOOT_IDS["spark1"], "spark2": "3" * 32}
    switcher = ProfileSwitcher(
        catalog=catalog_value,
        backend=FakeBackend(events),
        state_store=store,
        inventory_provider=lambda: inventory(boot_ids=new_boot_ids),
    )

    report = switcher.switch_profile("target")

    operations = [event[2][0] for event in events if event[0] == "remote"]
    assert report.status == "active"
    assert report.retained_workloads == ()
    assert "profile-stop" in operations
    assert "profile-start" in operations
    assert dict(store.state.boot_ids) == new_boot_ids


def test_activation_fails_closed_when_live_boot_id_is_missing() -> None:
    definition = workload()
    target = profile("target", definition)
    catalog_value = catalog(target, definition=definition)
    events: list[tuple] = []
    store = FakeStore(ControllerState.stopped(), events)
    incomplete = inventory()
    del incomplete["spark2"]["boot_id"]
    switcher = ProfileSwitcher(
        catalog=catalog_value,
        backend=FakeBackend(events),
        state_store=store,
        inventory_provider=lambda: incomplete,
    )

    report = switcher.switch_profile("target")

    assert report.status == "blocked"
    assert report.errors == ("live boot ID unavailable on spark2",)
    assert [event for event in events if event[0] == "remote"] == []
    assert store.saves == []


def test_activation_fails_closed_when_live_inventory_is_malformed() -> None:
    definition = workload()
    target = profile("target", definition)
    catalog_value = catalog(target, definition=definition)
    events: list[tuple] = []
    store = FakeStore(ControllerState.stopped(), events)
    switcher = ProfileSwitcher(
        catalog=catalog_value,
        backend=FakeBackend(events),
        state_store=store,
        inventory_provider=lambda: None,  # type: ignore[return-value]
    )

    report = switcher.switch_profile("target")

    assert report.status == "blocked"
    assert report.errors == ("live inventory is malformed",)
    assert [event for event in events if event[0] == "remote"] == []
    assert store.saves == []


def test_failed_health_cleans_up_target_and_finishes_stopped() -> None:
    definition = workload("generator")
    target = profile("generator-only", definition)
    catalog_value = catalog(target, definition=definition)
    switcher, events, store = make_switcher(
        catalog_value,
        ControllerState.stopped(),
        fail=("profile-health", "generator"),
    )

    report = switcher.switch_profile("generator-only")

    assert report.status == "stopped"
    assert report.published_endpoints == {}
    operations = [event[2][0] for event in events if event[0] == "remote"]
    assert operations[-2:] == ["profile-stop", "profile-verify-release"]
    assert store.state.status == "stopped"
    assert "fixture failure" in (store.state.last_error or "")


def test_failed_distributed_start_still_runs_full_ordered_cleanup() -> None:
    definition = workload("generator", distributed=True)
    target = profile("generator-only", definition)
    catalog_value = catalog(target, definition=definition)
    switcher, events, store = make_switcher(
        catalog_value,
        ControllerState.stopped(),
        fail=("profile-start", "generator"),
    )

    report = switcher.switch_profile("generator-only")

    assert report.status == "stopped"
    cleanup = [
        event
        for event in events
        if event[0] == "remote"
        and event[2][0] in {"profile-stop", "profile-verify-release"}
    ]
    assert cleanup == [
        ("remote", "spark1", ("profile-stop", "generator", "head")),
        ("remote", "spark2", ("profile-stop", "generator", "worker")),
        ("remote", "spark1", ("profile-verify-release", "generator", "head")),
        ("remote", "spark2", ("profile-verify-release", "generator", "worker")),
    ]
    assert store.state.status == "stopped"


def test_final_state_save_failure_stops_distributed_workload_in_order() -> None:
    definition = workload("generator", distributed=True)
    target = profile("generator-only", definition)
    catalog_value = catalog(target, definition=definition)
    events: list[tuple] = []
    store = FailingSaveStore(
        ControllerState.stopped(), events, fail_from_attempt=2, fail_once=True
    )
    switcher = ProfileSwitcher(
        catalog=catalog_value,
        backend=FakeBackend(events),
        state_store=store,
        inventory_provider=inventory,
    )

    report = switcher.switch_profile("generator-only")

    assert report.status == "stopped"
    assert "fixture save failure 2" in " ".join(report.errors)
    cleanup = [
        event
        for event in events
        if event[0] == "remote"
        and event[2][0] in {"profile-stop", "profile-verify-release"}
    ]
    assert cleanup == [
        ("remote", "spark1", ("profile-stop", "generator", "head")),
        ("remote", "spark2", ("profile-stop", "generator", "worker")),
        ("remote", "spark1", ("profile-verify-release", "generator", "head")),
        ("remote", "spark2", ("profile-verify-release", "generator", "worker")),
    ]
    assert store.state.status == "stopped"


def test_recovery_save_failure_returns_degraded_after_cleanup() -> None:
    definition = workload("generator", distributed=True)
    target = profile("generator-only", definition)
    catalog_value = catalog(target, definition=definition)
    events: list[tuple] = []
    store = FailingSaveStore(ControllerState.stopped(), events, fail_from_attempt=2)
    switcher = ProfileSwitcher(
        catalog=catalog_value,
        backend=FakeBackend(events),
        state_store=store,
        inventory_provider=inventory,
    )

    report = switcher.switch_profile("generator-only")

    assert report.status == "degraded"
    assert report.published_endpoints == {}
    assert report.output_provenance == {}
    assert "fixture save failure 2" in " ".join(report.errors)
    assert "fixture save failure 3" in " ".join(report.errors)
    assert len(" ".join(report.errors)) <= 2 * 2_048
    cleanup_nodes = [
        event[1]
        for event in events
        if event[0] == "remote" and event[2][0] == "profile-stop"
    ]
    assert cleanup_nodes == ["spark1", "spark2"]
    assert store.state.status == "transitioning"


def test_final_state_save_failure_never_reports_active_endpoints() -> None:
    definition = workload("generator")
    target = profile("generator-only", definition)
    catalog_value = catalog(target, definition=definition)
    events: list[tuple] = []
    store = FailingSaveStore(
        ControllerState.stopped(), events, fail_from_attempt=2, fail_once=True
    )
    switcher = ProfileSwitcher(
        catalog=catalog_value,
        backend=FakeBackend(events),
        state_store=store,
        inventory_provider=inventory,
    )

    report = switcher.switch_profile("generator-only")

    assert report.status == "stopped"
    assert report.published_endpoints == {}
    assert report.output_provenance == {}
    assert store.state.active_profile is None


def test_unreconciled_degraded_state_blocks_activation() -> None:
    definition = workload()
    target = profile("target", definition)
    catalog_value = catalog(target, definition=definition)
    degraded = ControllerState(
        status="degraded",
        active_profile=None,
        target_profile="old-target",
        restore_profile=None,
        last_error="unknown remote process state",
    )
    switcher, events, store = make_switcher(catalog_value, degraded)

    report = switcher.switch_profile("target")

    assert report.status == "blocked"
    assert "manual recovery" in " ".join(report.errors)
    assert [event for event in events if event[0] == "remote"] == []
    assert store.saves == []


def test_restore_intent_is_persisted_but_never_runs_automatically() -> None:
    definition = workload()
    home = profile("home", definition)
    temporary = replace(home, id="temporary")
    catalog_value = catalog(home, temporary, definition=definition)
    switcher, events, store = make_switcher(catalog_value, ControllerState.stopped())

    report = switcher.switch_profile("temporary", restore_to="default")

    assert report.target_profile == "temporary"
    assert report.profile_sha256 == fingerprint(temporary)
    assert report.definition_sha256 == {definition.id: fingerprint(definition)}
    assert report.output_provenance["profile_sha256"] == fingerprint(temporary)
    assert report.restore_profile == "home"
    assert store.state.active_profile == "temporary"
    assert store.state.restore_profile == "home"
    assert [event[2][0] for event in events if event[0] == "remote"].count(
        "profile-infer"
    ) == 1

    restored = switcher.switch_profile("default")

    assert restored.target_profile == "home"
    assert store.state.active_profile == "home"


def test_explicit_restoration_is_readmitted_and_can_fail_separately() -> None:
    definition = workload()
    home = profile("home", definition)
    temporary = replace(home, id="temporary")
    catalog_value = catalog(home, temporary, definition=definition)
    events: list[tuple] = []
    store = FakeStore(ControllerState.stopped(), events)

    def block_home(profile_value, catalog_value, inventory_value):
        if profile_value.id == "home":
            return AdmissionReport(("home restoration evidence expired",))
        return check_admission(profile_value, catalog_value, inventory_value)

    switcher = ProfileSwitcher(
        catalog=catalog_value,
        backend=FakeBackend(events),
        state_store=store,
        inventory_provider=inventory,
        admission_checker=block_home,
    )

    report = switcher.switch_profile("temporary", restore_to="default")
    restored = switcher.switch_profile("default")

    assert report.status == "active"
    assert report.profile_sha256 == fingerprint(temporary)
    assert restored.status == "blocked"
    assert restored.errors == ("home restoration evidence expired",)
    assert store.state.active_profile == "temporary"
    assert store.state.active_profile_sha256 == fingerprint(temporary)


def test_dry_run_reports_restore_intent_without_executing_restoration() -> None:
    definition = workload()
    home = profile("home", definition)
    temporary = replace(home, id="temporary")
    catalog_value = catalog(home, temporary, definition=definition)
    switcher, events, store = make_switcher(catalog_value, ControllerState.stopped())

    report = switcher.switch_profile("temporary", restore_to="default", dry_run=True)

    assert report.status == "planned"
    assert report.restore_profile == "home"
    assert events == []
    assert store.saves == []


def test_active_content_mismatch_blocks_before_remote_or_state_mutation() -> None:
    definition = workload()
    current = profile("current", definition)
    target = replace(current, id="target")
    catalog_value = catalog(current, target, definition=definition)
    stale = replace(
        active_state(current, definition),
        active_profile_sha256="f" * 64,
    )
    switcher, events, store = make_switcher(catalog_value, stale)

    report = switcher.switch_profile("target")

    assert report.status == "blocked"
    assert report.errors == (
        "persisted active profile fingerprint does not match catalog; manual recovery required",
    )
    assert events == [("lock",)]
    assert store.saves == []


def test_active_definition_set_or_hash_drift_blocks_before_mutation() -> None:
    definition = workload()
    current = profile("current", definition)
    target = replace(current, id="target")
    catalog_value = catalog(current, target, definition=definition)
    stale = replace(
        active_state(current, definition),
        active_definition_sha256={definition.id: "f" * 64, "unknown-old": "e" * 64},
    )
    switcher, events, store = make_switcher(catalog_value, stale)

    report = switcher.switch_profile("target")

    assert report.status == "blocked"
    assert report.errors == (
        "persisted active definition fingerprints do not match catalog; manual recovery required",
    )
    assert events == [("lock",)]
    assert store.saves == []


def test_unknown_current_definition_blocks_without_using_new_commands() -> None:
    definition = workload()
    target = profile("target", definition)
    unknown_current = replace(
        target,
        id="unknown-current",
        placements={"spark1": (), "spark2": ("removed-runtime",)},
        endpoints={},
    )
    catalog_value = catalog(unknown_current, target, definition=definition)
    state = ControllerState(
        status="active",
        active_profile="unknown-current",
        target_profile=None,
        restore_profile=None,
        last_error=None,
        active_profile_sha256=fingerprint(unknown_current),
        active_definition_sha256={"removed-runtime": "d" * 64},
    )
    switcher, events, store = make_switcher(catalog_value, state)

    report = switcher.switch_profile("target")

    assert report.status == "blocked"
    assert report.errors == (
        "persisted active profile references unknown workload: removed-runtime; manual recovery required",
    )
    assert events == [("lock",)]
    assert store.saves == []


def test_unknown_target_definition_is_a_stable_block_not_key_error() -> None:
    definition = workload()
    valid = profile("valid", definition)
    unknown = replace(
        valid,
        id="unknown",
        placements={"spark1": (), "spark2": ("missing",)},
        endpoints={},
    )
    catalog_value = catalog(valid, unknown, definition=definition)
    switcher, events, store = make_switcher(catalog_value, ControllerState.stopped())

    report = switcher.switch_profile("unknown")

    assert report.status == "blocked"
    assert "unknown workload: missing" in report.errors
    assert events == [("lock",)]
    assert store.saves == []


def test_retained_workload_gets_final_health_and_quality_gate_after_residency() -> None:
    definition = workload()
    current = profile("current", definition)
    target = replace(current, id="target")
    catalog_value = catalog(current, target, definition=definition)
    switcher, events, _ = make_switcher(
        catalog_value, active_state(current, definition)
    )

    report = switcher.switch_profile("target")

    assert report.status == "active"
    assert [event[2][0] for event in events if event[0] == "remote"] == [
        "profile-health",
        "profile-health",
        "profile-infer",
    ]


def test_unexpected_backend_exception_is_normalized_and_cleanup_continues() -> None:
    definition = workload("generator", distributed=True)
    target = profile("target", definition)
    catalog_value = catalog(target, definition=definition)
    events: list[tuple] = []
    store = FakeStore(ControllerState.stopped(), events)

    class RaisingBackend(FakeBackend):
        def run(self, node, argv, timeout):
            self.events.append(("remote", node, argv))
            if argv[0] == "profile-health":
                raise RuntimeError("runtime exploded")
            if argv[0] == "profile-stop" and node == "spark1":
                raise OSError("one cleanup node failed")
            return command_result()

    switcher = ProfileSwitcher(
        catalog=catalog_value,
        backend=RaisingBackend(events),
        state_store=store,
        inventory_provider=inventory,
    )

    report = switcher.switch_profile("target")

    assert report.status == "degraded"
    assert "runtime exploded" in " ".join(report.errors)
    assert len(report.errors[0]) <= 2_048
    cleanup_nodes = [
        event[1]
        for event in events
        if event[0] == "remote" and event[2][0] == "profile-stop"
    ]
    assert cleanup_nodes == ["spark1", "spark2"]
    assert store.state.status == "degraded"


def test_read_only_workload_health_probe_reports_adapter_failure() -> None:
    definition = workload("generator")
    target = profile("target", definition)
    catalog_value = catalog(target, definition=definition)
    switcher, events, store = make_switcher(
        catalog_value,
        ControllerState.stopped(),
        fail=("profile-health", "generator"),
    )

    healthy = switcher.workload_is_healthy("generator")

    assert healthy is False
    assert [event[2][0] for event in events if event[0] == "remote"] == [
        "profile-health"
    ]
    assert store.saves == []
