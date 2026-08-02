"""Developer-machine command line controller for Spark profiles."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
import tomllib
from typing import Callable, Mapping, Protocol, Sequence

from .admission import check_admission
from .backend import SshBackend
from .catalog import Catalog
from .state import ControllerState, LockBusy, LockNotStale, StateError, StateStore
from .switcher import ProfileSwitcher, SwitchReport


_MAX_TEXT_CHARS = 1_024
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|api[_-]?key|password|secret|token)\b"
    r"(\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")


class _UsageError(ValueError):
    pass


class _CliParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


class _StateStore(Protocol):
    def load(self) -> ControllerState: ...

    def break_stale_lock(self) -> bool: ...


@dataclass(frozen=True)
class CliDependencies:
    catalog: Catalog
    state_store: _StateStore
    switcher: ProfileSwitcher
    inventory_provider: Callable[[], Mapping[str, object]]


def build_dependencies(
    root: Path | None = None,
    *,
    state_directory: Path | None = None,
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
    catalog = Catalog.load(repository_root)

    def conservative_inventory() -> Mapping[str, object]:
        # Live node-health collection is a later explicit integration. Missing
        # measurements fail admission closed and never contact a remote node.
        return {"spark1": {}, "spark2": {}}

    switcher = ProfileSwitcher(
        catalog=catalog,
        backend=backend,
        state_store=store,
        inventory_provider=conservative_inventory,
    )
    return CliDependencies(catalog, store, switcher, conservative_inventory)


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
            {"error": str(error), "error_type": "arguments"},
            error_args,
        )
        return 2
    if dependencies is None:
        try:
            dependencies = build_dependencies(root)
        except (OSError, KeyError, TypeError, ValueError) as error:
            _emit(
                {
                    "error": f"cannot load controller configuration: {error}",
                    "error_type": "configuration",
                },
                args,
            )
            return 2
    if args.command == "break-stale-lock":
        try:
            broken = dependencies.state_store.break_stale_lock()
        except (LockBusy, LockNotStale) as error:
            payload = {"error": str(error), "error_type": "lock_conflict"}
            _emit(payload, args)
            return 7
        except StateError as error:
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
        except StateError as error:
            _emit(
                {"error": str(error), "error_type": "configuration"}, args
            )
            return 2
        payload = state.to_dict()
        payload["published_endpoints"] = (
            dict(dependencies.catalog.profiles[state.active_profile].endpoints)
            if _active_state_matches_catalog(state, dependencies.catalog)
            else {}
        )
        _emit(payload, args)
        return 0

    if args.command == "endpoint":
        try:
            state = dependencies.state_store.load()
        except StateError as error:
            _emit(
                {"error": str(error), "error_type": "configuration"}, args
            )
            return 2
        if state.status != "active":
            payload = {
                "available": False,
                "endpoint": args.name,
                "reason": f"controller status is {state.status}",
            }
            _emit(payload, args)
            return 3
        if not _active_state_matches_catalog(state, dependencies.catalog):
            _emit(
                {
                    "available": False,
                    "endpoint": args.name,
                    "reason": (
                        "active controller fingerprints do not match the catalog"
                    ),
                },
                args,
            )
            return 3
        profile = dependencies.catalog.profiles[state.active_profile]
        if args.name not in profile.endpoints:
            _emit(
                {
                    "available": False,
                    "endpoint": args.name,
                    "reason": (
                        "endpoint is not published by active profile "
                        f"{profile.id}"
                    ),
                },
                args,
            )
            return 3
        workload_id = profile.endpoints[args.name]
        definition = dependencies.catalog.definitions[workload_id]
        nodes = sorted(
            node
            for node, workloads in profile.placements.items()
            if workload_id in workloads
        )
        payload = {
            "available": True,
            "endpoint": args.name,
            "host": definition.endpoint.host,
            "nodes": nodes,
            "port": definition.endpoint.port,
            "profile_id": profile.id,
            "workload_id": workload_id,
        }
        _emit(payload, args)
        return 0

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
        report = check_admission(
            profile, dependencies.catalog, dependencies.inventory_provider()
        )
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
    except StateError as error:
        _emit({"error": str(error), "error_type": "configuration"}, args)
        return 2
    payload = _switch_payload(report)
    _emit(payload, args)
    if report.errors and report.status in {"stopped", "degraded"}:
        return 6
    if report.errors or report.status == "blocked":
        return 3
    return 0
