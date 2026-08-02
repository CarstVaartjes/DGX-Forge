"""Fail-closed reconciliation of whole-cluster Spark profiles."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from .admission import AdmissionReport, check_admission
from .backend import CommandResult, SshBackend
from .catalog import Catalog, fingerprint
from .contracts import ClusterProfile, WorkloadDefinition
from .state import ControllerState, StateStore

_MAX_ERROR_CHARS = 2_048
_MAX_DIAGNOSTICS = 64


class _StateStore(Protocol):
    def acquire(self): ...

    def load(self) -> ControllerState: ...

    def save(self, state: ControllerState) -> None: ...

    def begin_transition(self) -> None: ...

    def finish_transition(self, state: ControllerState) -> None: ...


@dataclass(frozen=True)
class Diagnostic:
    operation: str
    workload: str
    node: str
    detail: str


@dataclass(frozen=True)
class SwitchReport:
    target_profile: str
    status: str
    profile_sha256: str
    definition_sha256: Mapping[str, str]
    published_endpoints: Mapping[str, str]
    restore_profile: str | None = None
    retained_workloads: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    output_provenance: Mapping[str, object] = field(default_factory=dict)
    dry_run: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "definition_sha256", MappingProxyType(dict(self.definition_sha256))
        )
        object.__setattr__(
            self,
            "published_endpoints",
            MappingProxyType(dict(self.published_endpoints)),
        )
        object.__setattr__(
            self, "output_provenance", MappingProxyType(dict(self.output_provenance))
        )


InventoryProvider = Callable[[], Mapping[str, object]]
AdmissionChecker = Callable[
    [ClusterProfile, Catalog, Mapping[str, object]], AdmissionReport
]


class _TransitionFailure(RuntimeError):
    pass


class ProfileSwitcher:
    """Reconcile one complete accepted profile while holding the local lock."""

    def __init__(
        self,
        *,
        catalog: Catalog,
        backend: SshBackend,
        state_store: StateStore | _StateStore,
        inventory_provider: InventoryProvider,
        admission_checker: AdmissionChecker = check_admission,
        timeout_seconds: float = 120,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        self.catalog = catalog
        self.backend = backend
        self.state_store = state_store
        self.inventory_provider = inventory_provider
        self.admission_checker = admission_checker
        self.timeout_seconds = timeout_seconds

    def switch_profile(
        self,
        target_id: str,
        *,
        restore_to: str | None = None,
        dry_run: bool = False,
    ) -> SwitchReport:
        """Activate one profile and persist, but never execute, restore intent."""
        if dry_run:
            return self._switch_with_state(
                self.state_store.load(),
                target_id,
                restore_to=restore_to,
                dry_run=True,
            )
        with self.state_store.acquire() as state:
            return self._switch_with_state(
                state,
                target_id,
                restore_to=restore_to,
                dry_run=False,
            )

    def workload_is_healthy(self, definition_id: str) -> bool:
        """Run only the declared read-only health gate for one workload."""
        definition = self.catalog.definitions.get(definition_id)
        if definition is None:
            return False
        diagnostics: list[Diagnostic] = []
        try:
            self._health_definition(definition, diagnostics)
        except _TransitionFailure:
            return False
        return True

    def _switch_with_state(
        self,
        state: ControllerState,
        target_id: str,
        *,
        restore_to: str | None,
        dry_run: bool,
    ) -> SwitchReport:
        target = self._resolve(target_id)
        restore_target = self._resolve(restore_to) if restore_to is not None else None
        return self._transition(
            state,
            target,
            restore_profile=restore_target.id if restore_target is not None else None,
            dry_run=dry_run,
        )

    def _resolve(self, identifier: str | None) -> ClusterProfile:
        assert identifier is not None
        if identifier in self.catalog.selectors:
            return self.catalog.profiles[self.catalog.selectors[identifier]]
        try:
            return self.catalog.profiles[identifier]
        except KeyError as error:
            raise ValueError(
                f"unknown cluster profile or selector: {identifier}"
            ) from error

    def _transition(
        self,
        state: ControllerState,
        target: ClusterProfile,
        *,
        restore_profile: str | None,
        dry_run: bool,
    ) -> SwitchReport:
        target_definitions = self._profile_definitions(target)
        profile_hash = self.catalog.profile_fingerprints.get(target.id, "")
        target_content_error = self._target_content_error(
            target, target_definitions, profile_hash
        )
        target_hashes = {
            identifier: self.catalog.definition_fingerprints[identifier]
            for identifier in target_definitions
            if identifier in self.catalog.definition_fingerprints
        }
        if target_content_error is not None:
            return SwitchReport(
                target_profile=target.id,
                status="blocked",
                profile_sha256=profile_hash,
                definition_sha256=target_hashes,
                published_endpoints={},
                restore_profile=restore_profile,
                errors=(target_content_error,),
                dry_run=dry_run,
            )
        state_error = self._state_error(state)
        if state_error is not None:
            return SwitchReport(
                target_profile=target.id,
                status="blocked",
                profile_sha256=profile_hash,
                definition_sha256=target_hashes,
                published_endpoints={},
                restore_profile=restore_profile,
                errors=(state_error,),
                dry_run=dry_run,
            )
        try:
            inventory = self.inventory_provider()
        except Exception as error:
            detail = f"{type(error).__name__}: {error}"[:_MAX_ERROR_CHARS]
            return SwitchReport(
                target_profile=target.id,
                status="blocked",
                profile_sha256=profile_hash,
                definition_sha256=target_hashes,
                published_endpoints={},
                restore_profile=restore_profile,
                errors=(f"live inventory unavailable: {detail}",),
                dry_run=dry_run,
            )
        if not isinstance(inventory, Mapping):
            return SwitchReport(
                target_profile=target.id,
                status="blocked",
                profile_sha256=profile_hash,
                definition_sha256=target_hashes,
                published_endpoints={},
                restore_profile=restore_profile,
                errors=("live inventory is malformed",),
                dry_run=dry_run,
            )
        live_boot_ids, boot_error = self._live_boot_ids(inventory)
        if boot_error is not None:
            return SwitchReport(
                target_profile=target.id,
                status="blocked",
                profile_sha256=profile_hash,
                definition_sha256=target_hashes,
                published_endpoints={},
                restore_profile=restore_profile,
                errors=(boot_error,),
                dry_run=dry_run,
            )
        admission = self.admission_checker(target, self.catalog, inventory)
        if not admission.ok:
            return SwitchReport(
                target_profile=target.id,
                status="blocked",
                profile_sha256=profile_hash,
                definition_sha256=target_hashes,
                published_endpoints={},
                restore_profile=restore_profile,
                errors=admission.errors,
                dry_run=dry_run,
            )
        if dry_run:
            return SwitchReport(
                target_profile=target.id,
                status="planned",
                profile_sha256=profile_hash,
                definition_sha256=target_hashes,
                published_endpoints={},
                restore_profile=restore_profile,
                dry_run=True,
            )

        diagnostics: list[Diagnostic] = []
        current = self.catalog.profiles.get(state.active_profile or "")
        retained = self._healthy_retained(
            state, current, target, live_boot_ids, diagnostics
        )

        # An active profile is the publication source. Clearing it withdraws all
        # changed aliases before the first stop command is issued.
        self.state_store.save(
            ControllerState(
                status="transitioning",
                active_profile=None,
                target_profile=target.id,
                restore_profile=restore_profile,
                last_error=None,
                boot_ids=live_boot_ids,
            )
        )
        self.state_store.begin_transition()

        live_workloads = (
            self._profile_definitions(current) if current is not None else set()
        )
        outputs: dict[str, str] = {}
        try:
            if current is not None:
                for identifier in self._workload_order(current, reverse=True):
                    if identifier in retained:
                        continue
                    self._stop_definition(
                        self.catalog.definitions[identifier], diagnostics
                    )
                    live_workloads.discard(identifier)

            for identifier in self._workload_order(target):
                if identifier in retained:
                    continue
                definition = self.catalog.definitions[identifier]
                self._verify_definition(definition, diagnostics)
                # A failed start may still have created a remote process. Mark
                # it live before issuing the command so cleanup is conservative.
                live_workloads.add(identifier)
                self._start_definition(definition, diagnostics)

            # Publication is gated only after the complete target residency is
            # established. Retained workloads receive these final gates too.
            for identifier in self._workload_order(target):
                definition = self.catalog.definitions[identifier]
                self._health_definition(definition, diagnostics)
                outputs[identifier] = self._infer_definition(definition, diagnostics)

            status = "active" if target_definitions else "stopped"
            if status == "active":
                final_state = ControllerState(
                    status="active",
                    active_profile=target.id,
                    target_profile=None,
                    restore_profile=restore_profile,
                    last_error=None,
                    active_profile_sha256=profile_hash,
                    active_definition_sha256=target_hashes,
                    boot_ids=live_boot_ids,
                )
                endpoints = dict(target.endpoints)
            else:
                final_state = ControllerState(
                    status="stopped",
                    active_profile=None,
                    target_profile=None,
                    restore_profile=restore_profile,
                    last_error=None,
                    boot_ids=live_boot_ids,
                )
                endpoints = {}
            try:
                self.state_store.finish_transition(final_state)
            except OSError as error:
                detail = f"{type(error).__name__}: {error}"[:_MAX_ERROR_CHARS]
                raise _TransitionFailure(
                    f"final state persistence failed: {detail}"
                ) from error
            provenance: dict[str, object] = {
                "profile": target.id,
                "profile_sha256": profile_hash,
                "definition_sha256": dict(target_hashes),
                "infer_outputs": outputs,
            }
            return SwitchReport(
                target_profile=target.id,
                status=status,
                profile_sha256=profile_hash,
                definition_sha256=target_hashes,
                published_endpoints=endpoints,
                restore_profile=restore_profile,
                retained_workloads=tuple(sorted(retained)),
                diagnostics=tuple(diagnostics),
                output_provenance=provenance,
            )
        except _TransitionFailure as error:
            cleanup_ok = self._cleanup_live(
                current, target, live_workloads, diagnostics
            )
            message = str(error)[:_MAX_ERROR_CHARS]
            status = "stopped" if cleanup_ok else "degraded"
            if status == "stopped":
                failed_state = ControllerState(
                    status="stopped",
                    active_profile=None,
                    target_profile=target.id,
                    restore_profile=restore_profile,
                    last_error=message,
                    boot_ids=live_boot_ids,
                )
            else:
                failed_state = ControllerState(
                    status="degraded",
                    active_profile=None,
                    target_profile=target.id,
                    restore_profile=restore_profile,
                    last_error=message,
                    boot_ids=live_boot_ids,
                )
            errors = [message]
            try:
                self.state_store.finish_transition(failed_state)
            except OSError as error:
                detail = f"{type(error).__name__}: {error}"[:_MAX_ERROR_CHARS]
                errors.append(
                    f"recovery state persistence failed: {detail}"[:_MAX_ERROR_CHARS]
                )
                status = "degraded"
                conservative_state = ControllerState(
                    status="degraded",
                    active_profile=None,
                    target_profile=target.id,
                    restore_profile=restore_profile,
                    last_error="; ".join(errors)[:_MAX_ERROR_CHARS],
                    boot_ids=live_boot_ids,
                )
                try:
                    # The failed final save may have raised after atomically
                    # replacing state.json. Make one bounded extra attempt so
                    # a transient recovery-write failure cannot leave that
                    # active publication consumable after cleanup.
                    self.state_store.finish_transition(conservative_state)
                except OSError as fallback_error:
                    fallback_detail = (
                        f"{type(fallback_error).__name__}: {fallback_error}"
                    )[:_MAX_ERROR_CHARS]
                    errors.append(
                        f"conservative state persistence failed: {fallback_detail}"[
                            :_MAX_ERROR_CHARS
                        ]
                    )
            return SwitchReport(
                target_profile=target.id,
                status=status,
                profile_sha256=profile_hash,
                definition_sha256=target_hashes,
                published_endpoints={},
                restore_profile=restore_profile,
                errors=tuple(errors),
                diagnostics=tuple(diagnostics),
            )

    def _target_content_error(
        self,
        target: ClusterProfile,
        identifiers: set[str],
        profile_hash: str,
    ) -> str | None:
        unknown = sorted(
            identifier
            for identifier in identifiers
            if identifier not in self.catalog.definitions
        )
        if unknown:
            return f"unknown workload: {unknown[0]}"
        if not profile_hash:
            return "target profile fingerprint is missing; manual recovery required"
        if profile_hash != fingerprint(target):
            return "target profile fingerprint does not match catalog; manual recovery required"
        for identifier in sorted(identifiers):
            definition_hash = self.catalog.definition_fingerprints.get(identifier)
            if definition_hash is None:
                return f"target definition fingerprint is missing: {identifier}"
            if definition_hash != fingerprint(self.catalog.definitions[identifier]):
                return (
                    f"target definition fingerprint does not match catalog: {identifier}; "
                    "manual recovery required"
                )
        return None

    def _state_error(self, state: ControllerState) -> str | None:
        if (
            state.active_profile is not None
            and state.active_profile not in self.catalog.profiles
        ):
            return "persisted active profile is absent from the catalog; manual recovery required"
        if state.status in {"transitioning", "degraded"}:
            return f"controller state is {state.status}; manual recovery required"
        if state.active_profile is None:
            if state.status == "active":
                return "controller state is active without a profile; manual recovery required"
            return None
        if state.status != "active":
            return (
                f"controller state is {state.status} with an active profile; "
                "manual recovery required"
            )
        current = self.catalog.profiles[state.active_profile]
        expected_profile_hash = self.catalog.profile_fingerprints.get(current.id)
        if expected_profile_hash != fingerprint(current):
            return "catalog active profile content is inconsistent; manual recovery required"
        if state.active_profile_sha256 != expected_profile_hash:
            return (
                "persisted active profile fingerprint does not match catalog; "
                "manual recovery required"
            )
        identifiers = self._profile_definitions(current)
        unknown = sorted(
            identifier
            for identifier in identifiers
            if identifier not in self.catalog.definitions
        )
        if unknown:
            return (
                f"persisted active profile references unknown workload: {unknown[0]}; "
                "manual recovery required"
            )
        missing_fingerprints = sorted(
            identifier
            for identifier in identifiers
            if identifier not in self.catalog.definition_fingerprints
        )
        if missing_fingerprints:
            return (
                "catalog active definition fingerprint is missing: "
                f"{missing_fingerprints[0]}; manual recovery required"
            )
        expected_definitions = {
            identifier: self.catalog.definition_fingerprints[identifier]
            for identifier in identifiers
        }
        if any(
            expected_definitions[identifier]
            != fingerprint(self.catalog.definitions[identifier])
            for identifier in identifiers
        ):
            return "catalog active definition content is inconsistent; manual recovery required"
        if dict(state.active_definition_sha256) != expected_definitions:
            return (
                "persisted active definition fingerprints do not match catalog; "
                "manual recovery required"
            )
        return None

    def _profile_definitions(self, profile: ClusterProfile) -> set[str]:
        return {
            identifier
            for node in ("spark1", "spark2")
            for identifier in profile.placements[node]
        }

    @staticmethod
    def _live_boot_ids(
        inventory: Mapping[str, object],
    ) -> tuple[dict[str, str], str | None]:
        boot_ids: dict[str, str] = {}
        for node in ("spark1", "spark2"):
            measurement = inventory.get(node)
            boot_id = (
                measurement.get("boot_id")
                if isinstance(measurement, Mapping)
                else None
            )
            if not isinstance(boot_id, str) or not boot_id:
                return {}, f"live boot ID unavailable on {node}"
            boot_ids[node] = boot_id
        return boot_ids, None

    def _workload_order(
        self, profile: ClusterProfile, *, reverse: bool = False
    ) -> tuple[str, ...]:
        identifiers: list[str] = []
        nodes = ("spark1", "spark2") if reverse else ("spark2", "spark1")
        for node in nodes:
            values = (
                reversed(profile.placements[node])
                if reverse
                else profile.placements[node]
            )
            for identifier in values:
                if identifier not in identifiers:
                    identifiers.append(identifier)
        return tuple(identifiers)

    def _healthy_retained(
        self,
        state: ControllerState,
        current: ClusterProfile | None,
        target: ClusterProfile,
        live_boot_ids: Mapping[str, str],
        diagnostics: list[Diagnostic],
    ) -> set[str]:
        if current is None:
            return set()
        if dict(state.boot_ids) != dict(live_boot_ids):
            return set()
        if state.active_profile_sha256 != self.catalog.profile_fingerprints.get(
            current.id
        ):
            return set()
        current_definitions = self._profile_definitions(current)
        target_definitions = self._profile_definitions(target)
        result: set[str] = set()
        for identifier in sorted(current_definitions & target_definitions):
            definition_hash = self.catalog.definition_fingerprints[identifier]
            if state.active_definition_sha256.get(identifier) != definition_hash:
                continue
            current_nodes = tuple(
                node
                for node in ("spark1", "spark2")
                if identifier in current.placements[node]
            )
            target_nodes = tuple(
                node
                for node in ("spark1", "spark2")
                if identifier in target.placements[node]
            )
            if current_nodes != target_nodes:
                continue
            current_aliases = sorted(
                alias
                for alias, target_id in current.endpoints.items()
                if target_id == identifier
            )
            target_aliases = sorted(
                alias
                for alias, target_id in target.endpoints.items()
                if target_id == identifier
            )
            if current_aliases != target_aliases:
                continue
            try:
                self._health_definition(
                    self.catalog.definitions[identifier], diagnostics
                )
            except _TransitionFailure:
                continue
            result.add(identifier)
        return result

    def _argv(
        self, definition: WorkloadDefinition, command: tuple[str, ...], node: str
    ) -> tuple[str, ...]:
        if definition.topology != "distributed":
            return command
        role = "head" if node == definition.stop_order[0] else "worker"
        return (*command, role)

    def _call(
        self,
        definition: WorkloadDefinition,
        operation: str,
        node: str,
        command: tuple[str, ...],
        diagnostics: list[Diagnostic],
    ) -> CommandResult:
        timeout = (
            definition.deadlines.for_operation(operation)
            if definition.deadlines is not None
            else self.timeout_seconds
        )
        try:
            result = self.backend.run(
                node, self._argv(definition, command, node), timeout
            )
        except Exception as error:
            detail = f"{type(error).__name__}: {error}"[:_MAX_ERROR_CHARS]
            self._diagnostic(diagnostics, operation, definition.id, node, detail)
            raise _TransitionFailure(
                f"{operation} failed for {definition.id} on {node}: {detail}"
            ) from error
        if not result.ok:
            detail = self._detail(result)
            self._diagnostic(diagnostics, operation, definition.id, node, detail)
            raise _TransitionFailure(
                f"{operation} failed for {definition.id} on {node}: {detail}"
            )
        return result

    def _verify_definition(
        self, definition: WorkloadDefinition, diagnostics: list[Diagnostic]
    ) -> None:
        for node in definition.start_order:
            self._call(
                definition, "verify", node, definition.commands.verify, diagnostics
            )

    def _start_definition(
        self, definition: WorkloadDefinition, diagnostics: list[Diagnostic]
    ) -> None:
        for node in definition.start_order:
            self._call(
                definition, "start", node, definition.commands.start, diagnostics
            )

    def _health_definition(
        self, definition: WorkloadDefinition, diagnostics: list[Diagnostic]
    ) -> None:
        for node in definition.start_order:
            self._call(
                definition, "health", node, definition.commands.health, diagnostics
            )

    def _infer_definition(
        self, definition: WorkloadDefinition, diagnostics: list[Diagnostic]
    ) -> str:
        node = definition.stop_order[0]
        result = self._call(
            definition, "infer", node, definition.commands.infer, diagnostics
        )
        return result.stdout.decode("utf-8", errors="replace")[:_MAX_ERROR_CHARS]

    def _stop_definition(
        self, definition: WorkloadDefinition, diagnostics: list[Diagnostic]
    ) -> None:
        for node in definition.stop_order:
            self._call(definition, "stop", node, definition.commands.stop, diagnostics)
        for node in definition.stop_order:
            self._call(
                definition,
                "verify-release",
                node,
                definition.commands.verify_release,
                diagnostics,
            )

    def _cleanup_live(
        self,
        current: ClusterProfile | None,
        target: ClusterProfile,
        identifiers: set[str],
        diagnostics: list[Diagnostic],
    ) -> bool:
        ok = True
        ordered: list[str] = []
        for profile in (target, current):
            if profile is None:
                continue
            for identifier in self._workload_order(profile, reverse=True):
                if identifier not in ordered:
                    ordered.append(identifier)
        for identifier in ordered:
            if identifier not in identifiers:
                continue
            definition = self.catalog.definitions[identifier]
            for node in definition.stop_order:
                try:
                    self._call(
                        definition, "stop", node, definition.commands.stop, diagnostics
                    )
                except _TransitionFailure:
                    ok = False
            for node in definition.stop_order:
                try:
                    self._call(
                        definition,
                        "verify-release",
                        node,
                        definition.commands.verify_release,
                        diagnostics,
                    )
                except _TransitionFailure:
                    ok = False
        return ok

    def _detail(self, result: CommandResult) -> str:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        timeout = "timeout" if result.timed_out else f"exit={result.returncode}"
        truncation = " (truncated)" if result.stderr_truncated else ""
        return f"{timeout}: {stderr or 'no diagnostic output'}{truncation}"[
            :_MAX_ERROR_CHARS
        ]

    def _diagnostic(
        self,
        diagnostics: list[Diagnostic],
        operation: str,
        workload: str,
        node: str,
        detail: str,
    ) -> None:
        if len(diagnostics) < _MAX_DIAGNOSTICS:
            diagnostics.append(Diagnostic(operation, workload, node, detail))
