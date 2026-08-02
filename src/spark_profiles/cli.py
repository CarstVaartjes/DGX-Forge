"""Developer-machine command line controller for Spark profiles."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import sys
import tomllib
from typing import Callable, Mapping, Protocol, Sequence

from .admission import check_admission, check_placement_policy
from .backend import SshBackend
from .catalog import Catalog
from .health import ClusterHealth, LocalHealthError, NodeHealthService
from .state import ControllerState, LockBusy, LockNotStale, StateError, StateStore
from .switcher import PrepareReport, ProfileSwitcher, SwitchReport


_MAX_TEXT_CHARS = 1_024
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|api[_-]?key|password|secret|token)\b"
    r"(\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_SENSITIVE_OPTION = re.compile(
    r"(?i)^--(?:[a-z0-9]+-)*(?:authorization|api-key|password|secret|token|private-key)(?:=|$)"
)


class _UsageError(ValueError):
    pass


class _CliParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


class _StateStore(Protocol):
    def acquire(self): ...

    def load(self) -> ControllerState: ...

    def break_stale_lock(self) -> bool: ...


class _HealthService(Protocol):
    def collect(self) -> ClusterHealth: ...


@dataclass(frozen=True)
class CliDependencies:
    catalog: Catalog
    state_store: _StateStore
    switcher: ProfileSwitcher
    inventory_provider: Callable[[], Mapping[str, object]]
    health_service: _HealthService | None = None


def build_dependencies(
    root: Path | None = None,
    *,
    state_directory: Path | None = None,
    include_health: bool = False,
) -> CliDependencies:
    """Build the production controller without contacting either Spark node."""
    repository_root = (
        Path(root).resolve()
        if root is not None
        else Path(__file__).resolve().parents[2]
    )
    with (repository_root / "config/controller.toml").open("rb") as source:
        configuration = tomllib.load(source)
    if configuration.get("schema_version") != 1:
        raise ValueError("unsupported controller configuration schema version")
    state_configuration = configuration["state"]
    ssh_configuration = configuration["ssh"]
    configured_state = Path(state_configuration["directory"])
    if configured_state.is_absolute() or ".." in configured_state.parts:
        raise ValueError("controller state directory must stay within the repository")
    store = StateStore(
        state_directory or repository_root / configured_state,
        stale_lock_seconds=state_configuration["stale_lock_seconds"],
    )
    backend = SshBackend(
        node_aliases=ssh_configuration["node_aliases"],
        connect_timeout_seconds=ssh_configuration["connect_timeout_seconds"],
        output_limit_bytes=ssh_configuration["output_limit_bytes"],
    )
    health_service = None
    if include_health:
        health_configuration = configuration["health"]
        health_backend = SshBackend(
            node_aliases=ssh_configuration["node_aliases"],
            connect_timeout_seconds=ssh_configuration["connect_timeout_seconds"],
            output_limit_bytes=health_configuration["max_output_bytes"],
        )
        health_service = NodeHealthService.from_repository(
            repository_root, health_backend
        )
    catalog = Catalog.load(repository_root)

    def conservative_inventory() -> Mapping[str, object]:
        return {"spark1": {}, "spark2": {}}

    def live_inventory() -> Mapping[str, object]:
        assert health_service is not None
        return _inventory_from_health(health_service.collect())

    inventory_provider = (
        live_inventory if health_service is not None else conservative_inventory
    )

    switcher = ProfileSwitcher(
        catalog=catalog,
        backend=backend,
        state_store=store,
        inventory_provider=inventory_provider,
    )
    return CliDependencies(
        catalog, store, switcher, inventory_provider, health_service
    )


def _inventory_from_health(health: ClusterHealth) -> Mapping[str, object]:
    """Project live health into the small admission/publication inventory."""
    inventory: dict[str, object] = {}
    for node_name in ("spark1", "spark2"):
        node = health.nodes[node_name]
        inventory[node_name] = {
            "healthy": node.status in {"healthy", "warning"},
            "free_memory_bytes": (
                node.memory.available_bytes if node.memory is not None else None
            ),
            "free_disk_bytes": (
                node.root_filesystem.available_bytes
                if node.root_filesystem is not None
                else None
            ),
            "boot_id": node.identity.boot_id if node.identity is not None else None,
        }
    return inventory


def _live_publication_error(
    state: ControllerState, inventory: Mapping[str, object]
) -> str | None:
    live_boot_ids: dict[str, str] = {}
    for node_name in ("spark1", "spark2"):
        measurement = inventory.get(node_name)
        if (
            not isinstance(measurement, Mapping)
            or measurement.get("healthy") is not True
        ):
            return "live Spark health gate failed"
        boot_id = measurement.get("boot_id")
        if not isinstance(boot_id, str) or not boot_id:
            return "live Spark health gate failed"
        live_boot_ids[node_name] = boot_id
    if dict(state.boot_ids) != live_boot_ids:
        return "Spark boot IDs changed since activation"
    return None


def _resolve_endpoint(
    name: str, dependencies: CliDependencies, state: ControllerState
) -> tuple[dict[str, object], int]:
    unavailable = lambda reason: (
        {"available": False, "endpoint": name, "reason": reason},
        3,
    )
    if state.status != "active":
        return unavailable(f"controller status is {state.status}")
    if not _active_state_matches_catalog(state, dependencies.catalog):
        return unavailable("active controller fingerprints do not match the catalog")
    if not _active_content_is_accepted(state, dependencies.catalog):
        return unavailable("active profile content is not currently accepted")
    profile = dependencies.catalog.profiles[state.active_profile]
    if not check_placement_policy(profile, dependencies.catalog).ok:
        return unavailable("active profile violates current placement policy")
    if name not in profile.endpoints:
        return unavailable(
            f"endpoint is not published by active profile {profile.id}"
        )
    try:
        inventory = dependencies.inventory_provider()
    except (OSError, RuntimeError, TypeError, ValueError):
        return unavailable("live Spark health gate failed")
    if not isinstance(inventory, Mapping):
        return unavailable("live Spark health gate failed")
    publication_error = _live_publication_error(state, inventory)
    if publication_error is not None:
        return unavailable(publication_error)
    workload_id = profile.endpoints[name]
    if not dependencies.switcher.workload_is_healthy(workload_id):
        return unavailable("active workload health gate failed")
    if dependencies.state_store.load() != state:
        return unavailable("controller state changed during endpoint check")
    definition = dependencies.catalog.definitions[workload_id]
    nodes = sorted(
        node
        for node, workloads in profile.placements.items()
        if workload_id in workloads
    )
    return (
        {
            "available": True,
            "endpoint": name,
            "host": definition.endpoint.host,
            "nodes": nodes,
            "port": definition.endpoint.port,
            "profile_id": profile.id,
            "workload_id": workload_id,
        },
        0,
    )


def _parser() -> argparse.ArgumentParser:
    parser = _CliParser(prog="sparkctl")
    parser.add_argument("--json", dest="global_json", action="store_true")
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=_CliParser
    )

    catalog = commands.add_parser("catalog")
    catalog.add_argument("--json", action="store_true")

    status = commands.add_parser("status")
    status.add_argument("--json", action="store_true")

    nodes = commands.add_parser("nodes")
    node_commands = nodes.add_subparsers(
        dest="nodes_command", required=True, parser_class=_CliParser
    )
    nodes_status = node_commands.add_parser("status")
    nodes_status.add_argument("--json", action="store_true")

    endpoint = commands.add_parser("endpoint")
    endpoint.add_argument("name")
    endpoint.add_argument("--json", action="store_true")

    validate = commands.add_parser("validate")
    validate.add_argument("selector")
    validate.add_argument("--json", action="store_true")

    switch = commands.add_parser("switch")
    switch.add_argument("selector")
    switch.add_argument("--dry-run", action="store_true")
    switch.add_argument("--restore")
    switch.add_argument("--json", action="store_true")

    prepare = commands.add_parser("prepare")
    prepare.add_argument("selector")
    prepare.add_argument("--json", action="store_true")

    restore_default = commands.add_parser("restore-default")
    restore_default.add_argument("--dry-run", action="store_true")
    restore_default.add_argument("--json", action="store_true")

    break_lock = commands.add_parser("break-stale-lock")
    break_lock.add_argument("--json", action="store_true")
    return parser


def _switch_payload(report: SwitchReport) -> dict[str, object]:
    return {
        "target_profile": report.target_profile,
        "status": report.status,
        "profile_sha256": report.profile_sha256,
        "definition_sha256": dict(report.definition_sha256),
        "published_endpoints": dict(report.published_endpoints),
        "restore_profile": report.restore_profile,
        "errors": list(report.errors[:16]),
        "dry_run": report.dry_run,
    }


def _prepare_payload(report: PrepareReport) -> dict[str, object]:
    return {
        "target_profile": report.target_profile,
        "status": report.status,
        "profile_sha256": report.profile_sha256,
        "definition_sha256": dict(report.definition_sha256),
        "resumable": report.resumable,
        "results": [asdict(result) for result in report.results],
        "errors": list(report.errors[:16]),
    }


def _active_state_matches_catalog(state: ControllerState, catalog: Catalog) -> bool:
    if state.status != "active" or state.active_profile not in catalog.profiles:
        return False
    profile = catalog.profiles[state.active_profile]
    identifiers = {
        definition_id
        for placements in profile.placements.values()
        for definition_id in placements
    }
    if any(identifier not in catalog.definition_fingerprints for identifier in identifiers):
        return False
    expected_definitions = {
        identifier: catalog.definition_fingerprints[identifier]
        for identifier in identifiers
    }
    return (
        state.active_profile_sha256 == catalog.profile_fingerprints.get(profile.id)
        and dict(state.active_definition_sha256) == expected_definitions
    )


def _active_content_is_accepted(state: ControllerState, catalog: Catalog) -> bool:
    if not _active_state_matches_catalog(state, catalog):
        return False
    identifiers = sorted(state.active_definition_sha256)
    for identifier in identifiers:
        definition_hash = catalog.definition_fingerprints[identifier]
        if (
            catalog.maturity.get(identifier) != "accepted"
            or catalog.maturity_fingerprints.get(identifier) != definition_hash
            or catalog.definitions[identifier].checkpoint.manifest_sha256 is None
        ):
            return False
    profile_hash = catalog.profile_fingerprints[state.active_profile]
    return catalog.accepted_profiles.get(profile_hash) == tuple(
        sorted(catalog.definition_fingerprints[identifier] for identifier in identifiers)
    )


def _sanitize_text(value: object) -> str:
    text = str(value).replace("\x00", "")
    text = _SENSITIVE_ASSIGNMENT.sub(lambda match: f"{match.group(1)}: <redacted>", text)
    text = _BEARER.sub("Bearer <redacted>", text)
    if "-----BEGIN " in text:
        text = text.split("-----BEGIN ", 1)[0] + "<redacted private key>"
    if len(text) > _MAX_TEXT_CHARS:
        text = text[: _MAX_TEXT_CHARS - 15] + "... (truncated)"
    return text


def _sanitize(value: object) -> object:
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, Mapping):
        return {str(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_sanitize(item) for item in value[:64]]
    return value


def _arguments_may_contain_secrets(argv: Sequence[str]) -> bool:
    return any(
        _SENSITIVE_OPTION.match(argument.replace("_", "-"))
        or _SENSITIVE_ASSIGNMENT.search(argument)
        or _BEARER.search(argument)
        or "-----BEGIN " in argument
        for argument in argv
    )


def _emit(payload: Mapping[str, object], args: argparse.Namespace) -> None:
    safe = _sanitize(payload)
    assert isinstance(safe, dict)
    json_mode = args.global_json or getattr(args, "json", False)
    if json_mode:
        print(json.dumps(safe, sort_keys=True, separators=(",", ":")))
        return
    priority = (
        "status",
        "target_profile",
        "profile_id",
        "endpoint",
        "available",
        "admitted",
        "broken",
        "reason",
        "error",
    )
    keys = [key for key in priority if key in safe]
    keys.extend(sorted(set(safe) - set(keys)))
    for key in keys:
        value = safe[key]
        if value is None:
            rendered = "-"
        elif isinstance(value, (dict, list)):
            rendered = json.dumps(value, sort_keys=True, separators=(",", ":"))
        else:
            rendered = str(value).lower() if isinstance(value, bool) else str(value)
        print(f"{key}: {rendered}")


def _human_bytes(value: object) -> str:
    if not isinstance(value, int):
        return "-"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    return f"{amount:.1f} {unit}" if unit != "B" else f"{value} B"


def _human_uptime(value: object) -> str:
    if not isinstance(value, int):
        return "-"
    days, remainder = divmod(value, 86400)
    hours = remainder // 3600
    return f"{days}d {hours:02d}h" if days else f"{hours}h"


def _health_table(result: ClusterHealth) -> str:
    headers = (
        "NODE", "STATE", "CPU", "LOAD1", "MEM AVAILABLE", "SWAP USED",
        "ROOT FREE", "GPU", "TEMP", "FABRIC", "UPTIME",
    )
    rows: list[tuple[str, ...]] = []
    details: list[str] = []
    for node_name in ("spark1", "spark2"):
        node = result.nodes[node_name]
        cpu = node.cpu
        memory = node.memory
        swap = node.swap
        root = node.root_filesystem
        accelerator = node.accelerator
        identity = node.identity
        functions = node.fabric.functions if node.fabric is not None else ()
        up = sum(
            1 for item in functions
            if item.operstate == "up" and item.carrier == 1
            and item.speed_mbps == 200000 and item.mtu == 1500
            and str(item.rdma_state).upper() == "ACTIVE"
            and item.rdma_interface == item.interface
        )
        cpu_value = cpu.utilization_percent if cpu is not None else None
        gpu_value = accelerator.utilization_percent if accelerator is not None else None
        temp_value = accelerator.temperature_c if accelerator is not None else None
        rows.append((
            node_name,
            node.status,
            f"{cpu_value:.1f}%" if isinstance(cpu_value, (int, float)) else "-",
            f"{cpu.load_1:.2f}" if cpu is not None and isinstance(cpu.load_1, (int, float)) else "-",
            _human_bytes(memory.available_bytes if memory is not None else None),
            _human_bytes(swap.used_bytes if swap is not None else None),
            _human_bytes(root.available_bytes if root is not None else None),
            f"{gpu_value:.1f}%" if isinstance(gpu_value, (int, float)) else "-",
            f"{temp_value:.1f} C" if isinstance(temp_value, (int, float)) else "-",
            f"{up}/{len(functions)} up" if functions else "-",
            _human_uptime(identity.uptime_seconds if identity is not None else None),
        ))
        if node.warnings:
            details.append(f"{node_name} warnings: {', '.join(node.warnings)}")
        if node.errors:
            details.append(f"{node_name} errors: {', '.join(node.errors)}")
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]
    render = lambda row: "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip()
    lines = [render(headers), *(render(row) for row in rows)]
    if details:
        lines.extend(("", *details))
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None,
    *,
    dependencies: CliDependencies | None = None,
    root: Path | None = None,
) -> int:
    raw_argv = tuple(argv) if argv is not None else tuple(sys.argv[1:])
    try:
        args = _parser().parse_args(raw_argv)
    except _UsageError as error:
        error_args = argparse.Namespace(
            global_json="--json" in raw_argv,
            json=False,
        )
        _emit(
            {
                "error": (
                    "invalid command arguments"
                    if _arguments_may_contain_secrets(raw_argv)
                    else str(error)
                ),
                "error_type": "arguments",
            },
            error_args,
        )
        return 2
    if dependencies is None:
        try:
            dependencies = build_dependencies(
                root,
                include_health=args.command
                in {"nodes", "endpoint", "validate", "switch", "restore-default"},
            )
        except LocalHealthError as error:
            _emit(
                {
                    "error": f"cannot load live node health: {error}",
                    "error_type": "health_configuration",
                },
                args,
            )
            return 5
        except (OSError, KeyError, TypeError, ValueError) as error:
            _emit(
                {
                    "error": f"cannot load controller configuration: {error}",
                    "error_type": "configuration",
                },
                args,
            )
            return 2
    if args.command == "nodes":
        if dependencies.health_service is None:
            _emit(
                {"error": "live node health service is unavailable", "error_type": "health_configuration"},
                args,
            )
            return 5
        try:
            health = dependencies.health_service.collect()
        except LocalHealthError as error:
            _emit(
                {"error": str(error), "error_type": "health_configuration"}, args
            )
            return 5
        if args.global_json or args.json:
            print(json.dumps(health.to_dict(), separators=(",", ":")))
        else:
            print(_health_table(health))
        return 4 if health.status == "critical" else 0
    if args.command == "break-stale-lock":
        try:
            broken = dependencies.state_store.break_stale_lock()
        except (LockBusy, LockNotStale) as error:
            payload = {"error": str(error), "error_type": "lock_conflict"}
            _emit(payload, args)
            return 7
        except (StateError, OSError) as error:
            _emit(
                {"error": str(error), "error_type": "configuration"}, args
            )
            return 2
        _emit({"broken": broken}, args)
        return 0

    if args.command == "catalog":
        profiles = []
        for profile_id, profile in sorted(dependencies.catalog.profiles.items()):
            workloads = sorted(
                {
                    definition_id
                    for placements in profile.placements.values()
                    for definition_id in placements
                }
            )
            profiles.append(
                {
                    "profile_id": profile_id,
                    "profile_sha256": dependencies.catalog.profile_fingerprints[
                        profile_id
                    ],
                    "workloads": workloads,
                    "endpoints": dict(profile.endpoints),
                }
            )
        definitions = [
            {
                "definition_id": definition_id,
                "definition_sha256": dependencies.catalog.definition_fingerprints[
                    definition_id
                ],
                "maturity": dependencies.catalog.maturity[definition_id],
            }
            for definition_id in sorted(dependencies.catalog.definitions)
        ]
        payload = {
            "selectors": dict(sorted(dependencies.catalog.selectors.items())),
            "profiles": profiles,
            "definitions": definitions,
        }
        _emit(payload, args)
        return 0

    if args.command == "status":
        try:
            state = dependencies.state_store.load()
        except (StateError, OSError) as error:
            _emit(
                {"error": str(error), "error_type": "configuration"}, args
            )
            return 2
        payload = state.to_dict()
        # This command is deliberately local. Only `endpoint` performs the
        # live publication gate, so local status never advertises availability.
        payload["published_endpoints"] = {}
        _emit(payload, args)
        return 0

    if args.command == "endpoint":
        try:
            with dependencies.state_store.acquire() as state:
                payload, exit_code = _resolve_endpoint(
                    args.name, dependencies, state
                )
                _emit(payload, args)
                return exit_code
        except (LockBusy, LockNotStale) as error:
            _emit({"error": str(error), "error_type": "lock_conflict"}, args)
            return 7
        except (StateError, OSError) as error:
            _emit(
                {"error": str(error), "error_type": "configuration"}, args
            )
            return 2

    if args.command == "validate":
        profile_id = dependencies.catalog.selectors.get(args.selector, args.selector)
        profile = dependencies.catalog.profiles.get(profile_id)
        if profile is None:
            _emit(
                {
                    "error": f"unknown cluster profile or selector: {args.selector}",
                    "error_type": "configuration",
                },
                args,
            )
            return 2
        try:
            inventory = dependencies.inventory_provider()
        except LocalHealthError as error:
            _emit(
                {"error": str(error), "error_type": "health_configuration"}, args
            )
            return 5
        except OSError as error:
            _emit(
                {"error": str(error), "error_type": "configuration"}, args
            )
            return 2
        if not isinstance(inventory, Mapping):
            _emit(
                {
                    "error": "live inventory is malformed",
                    "error_type": "configuration",
                },
                args,
            )
            return 2
        report = check_admission(profile, dependencies.catalog, inventory)
        payload = {
            "profile_id": profile.id,
            "valid": True,
            "admitted": report.ok,
            "profile_sha256": dependencies.catalog.profile_fingerprints[profile.id],
            "definition_sha256": {
                identifier: dependencies.catalog.definition_fingerprints[identifier]
                for identifier in sorted(
                    {
                        definition_id
                        for placements in profile.placements.values()
                        for definition_id in placements
                    }
                )
            },
            "errors": list(report.errors),
        }
        _emit(payload, args)
        return 0 if report.ok else 3

    if args.command == "prepare":
        target = dependencies.catalog.selectors.get(args.selector, args.selector)
        if target not in dependencies.catalog.profiles:
            _emit(
                {
                    "error": (
                        "unknown cluster profile or selector: "
                        f"{args.selector}"
                    ),
                    "error_type": "configuration",
                },
                args,
            )
            return 2
        try:
            report = dependencies.switcher.prepare_profile(target)
        except (LockBusy, LockNotStale) as error:
            _emit({"error": str(error), "error_type": "lock_conflict"}, args)
            return 7
        except (StateError, OSError) as error:
            _emit({"error": str(error), "error_type": "configuration"}, args)
            return 2
        _emit(_prepare_payload(report), args)
        return {
            "prepared": 0,
            "blocked": 3,
            "failed": 6,
            "in-progress": 8,
        }.get(report.status, 6)

    selector = "default" if args.command == "restore-default" else args.selector
    target = dependencies.catalog.selectors.get(selector, selector)
    if target not in dependencies.catalog.profiles:
        _emit(
            {
                "error": f"unknown cluster profile or selector: {selector}",
                "error_type": "configuration",
            },
            args,
        )
        return 2
    restore = (
        dependencies.catalog.selectors.get(args.restore, args.restore)
        if args.command == "switch" and args.restore is not None
        else None
    )
    if restore is not None and restore not in dependencies.catalog.profiles:
        _emit(
            {
                "error": f"unknown cluster profile or selector: {args.restore}",
                "error_type": "configuration",
            },
            args,
        )
        return 2
    try:
        report = dependencies.switcher.switch_profile(
            target, restore_to=restore, dry_run=args.dry_run
        )
    except (LockBusy, LockNotStale) as error:
        _emit({"error": str(error), "error_type": "lock_conflict"}, args)
        return 7
    except (StateError, OSError) as error:
        _emit({"error": str(error), "error_type": "configuration"}, args)
        return 2
    payload = _switch_payload(report)
    _emit(payload, args)
    if report.errors and report.status in {"stopped", "degraded"}:
        return 6
    if report.errors or report.status == "blocked":
        return 3
    return 0
