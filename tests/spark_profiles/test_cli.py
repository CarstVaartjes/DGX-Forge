from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, replace
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys

import pytest

from spark_profiles.catalog import Catalog, fingerprint
from spark_profiles.cli import CliDependencies, build_dependencies, main
from spark_profiles.state import ControllerState, LockBusy, LockNotStale, StateFormatError
from spark_profiles.switcher import SwitchReport


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class FakeStore:
    def __init__(
        self,
        state: ControllerState | None = None,
        *,
        stale_result: bool = False,
        stale_error: Exception | None = None,
    ) -> None:
        self.state = state or ControllerState.stopped()
        self.stale_result = stale_result
        self.stale_error = stale_error

    def load(self) -> ControllerState:
        return self.state

    def break_stale_lock(self) -> bool:
        if self.stale_error is not None:
            raise self.stale_error
        return self.stale_result


class FakeSwitcher:
    def switch_profile(
        self,
        target_id: str,
        *,
        restore_to: str | None = None,
        dry_run: bool = False,
    ) -> SwitchReport:
        return SwitchReport(
            target_profile=target_id,
            status="planned" if dry_run else "active",
            profile_sha256="a" * 64,
            definition_sha256={"fixture": "b" * 64},
            published_endpoints={},
            restore_profile=restore_to,
            dry_run=dry_run,
        )


@dataclass(frozen=True)
class Result:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def json(self) -> dict[str, object]:
        return json.loads(self.stdout)


def invoke(
    *argv: str,
    state: ControllerState | None = None,
    store: FakeStore | None = None,
    switcher: FakeSwitcher | None = None,
    catalog_value: Catalog | None = None,
) -> Result:
    catalog = catalog_value or Catalog.load(REPOSITORY_ROOT)
    dependencies = CliDependencies(
        catalog=catalog,
        state_store=store or FakeStore(state),
        switcher=switcher or FakeSwitcher(),
        inventory_provider=lambda: {},
    )
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(argv, dependencies=dependencies)
    return Result(exit_code, stdout.getvalue(), stderr.getvalue())


def accepted_catalog(
    *,
    matching_maturity_fingerprint: bool = True,
    manifest_digest: bool = True,
    profile_evidence: bool = True,
) -> Catalog:
    base = Catalog.load(REPOSITORY_ROOT)
    identifier = "deepseek-agent-dual"
    original = base.definitions[identifier]
    definition = replace(
        original,
        checkpoint=replace(
            original.checkpoint,
            manifest_sha256="9" * 64 if manifest_digest else None,
        ),
    )
    definition_hash = fingerprint(definition)
    profile_hash = base.profile_fingerprints["agent-full-dual"]
    return Catalog(
        definitions={identifier: definition},
        profiles=base.profiles,
        selectors=base.selectors,
        definition_fingerprints={identifier: definition_hash},
        profile_fingerprints=base.profile_fingerprints,
        maturity={identifier: "accepted"},
        maturity_fingerprints={
            identifier: definition_hash
            if matching_maturity_fingerprint
            else "f" * 64
        },
        accepted_profiles={
            profile_hash: (definition_hash,)
        }
        if profile_evidence
        else {},
    )


def active_state(catalog: Catalog) -> ControllerState:
    profile = catalog.profiles["agent-full-dual"]
    return ControllerState(
        status="active",
        active_profile=profile.id,
        target_profile=None,
        restore_profile=None,
        last_error=None,
        active_profile_sha256=catalog.profile_fingerprints[profile.id],
        active_definition_sha256={
            "deepseek-agent-dual": catalog.definition_fingerprints[
                "deepseek-agent-dual"
            ]
        },
    )


def test_agent_alias_resolves_to_full_default() -> None:
    result = invoke("switch", "agent", "--dry-run", "--json")

    assert result.exit_code == 0
    assert result.json["target_profile"] == "agent-full-dual"
    assert result.json["status"] == "planned"


def test_planned_home_is_visible_but_not_activatable() -> None:
    result = invoke("validate", "default", "--json")

    assert result.json["profile_id"] == "agent-full-dual"
    assert result.json["valid"] is True
    assert result.json["admitted"] is False
    assert "deepseek-agent-dual maturity is planned" in result.json["errors"]
    assert result.exit_code == 3


def test_endpoint_refuses_workload_when_controller_is_stopped() -> None:
    result = invoke("endpoint", "deepseek", "--json")

    assert result.exit_code == 3
    assert result.json == {
        "available": False,
        "endpoint": "deepseek",
        "reason": "controller status is stopped",
    }


def test_status_is_a_local_stopped_snapshot() -> None:
    result = invoke("status", "--json")

    assert result.exit_code == 0
    assert result.json["status"] == "stopped"
    assert result.json["active_profile"] is None
    assert result.json["published_endpoints"] == {}


def test_catalog_supports_global_json_and_shows_planned_profiles() -> None:
    result = invoke("--json", "catalog")
    per_command = invoke("catalog", "--json")

    assert result.exit_code == 0
    assert per_command.exit_code == 0
    assert per_command.json == result.json
    assert result.json["selectors"]["agent"] == "agent-full-dual"
    assert result.json["profiles"][0]["profile_id"] == "agent-full-dual"
    assert result.json["profiles"][0]["workloads"] == ["deepseek-agent-dual"]
    assert result.json["definitions"][0]["maturity"] == "planned"


def test_restore_default_is_an_explicit_ordinary_switch() -> None:
    result = invoke("restore-default", "--dry-run", "--json")

    assert result.exit_code == 0
    assert result.json["target_profile"] == "agent-full-dual"
    assert result.json["restore_profile"] is None
    assert result.json["dry_run"] is True


def test_switch_only_records_canonical_restore_intent() -> None:
    result = invoke(
        "switch", "agent", "--restore", "default", "--dry-run", "--json"
    )

    assert result.exit_code == 0
    assert result.json["target_profile"] == "agent-full-dual"
    assert result.json["restore_profile"] == "agent-full-dual"


def test_break_stale_lock_reports_whether_a_lock_was_removed() -> None:
    result = invoke(
        "break-stale-lock", "--json", store=FakeStore(stale_result=True)
    )

    assert result.exit_code == 0
    assert result.json == {"broken": True}


def test_break_stale_lock_refuses_an_unsafe_override() -> None:
    result = invoke(
        "break-stale-lock",
        "--json",
        store=FakeStore(stale_error=LockNotStale("lock records live PID 123")),
    )

    assert result.exit_code == 7
    assert result.json == {
        "error": "lock records live PID 123",
        "error_type": "lock_conflict",
    }


def test_endpoint_refuses_matching_but_planned_active_content() -> None:
    catalog = Catalog.load(REPOSITORY_ROOT)
    profile = catalog.profiles["agent-full-dual"]
    state = ControllerState(
        status="active",
        active_profile=profile.id,
        target_profile=None,
        restore_profile=None,
        last_error=None,
        active_profile_sha256=catalog.profile_fingerprints[profile.id],
        active_definition_sha256={
            "deepseek-agent-dual": catalog.definition_fingerprints[
                "deepseek-agent-dual"
            ]
        },
    )

    result = invoke("endpoint", "deepseek", "--json", state=state)

    assert result.exit_code == 3
    assert result.json == {
        "available": False,
        "endpoint": "deepseek",
        "reason": "active profile content is not currently accepted",
    }


def test_status_hides_matching_but_planned_active_endpoints() -> None:
    catalog = Catalog.load(REPOSITORY_ROOT)
    profile = catalog.profiles["agent-full-dual"]
    state = ControllerState(
        status="active",
        active_profile=profile.id,
        target_profile=None,
        restore_profile=None,
        last_error=None,
        active_profile_sha256=catalog.profile_fingerprints[profile.id],
        active_definition_sha256={
            "deepseek-agent-dual": catalog.definition_fingerprints[
                "deepseek-agent-dual"
            ]
        },
    )

    result = invoke("status", "--json", state=state)

    assert result.exit_code == 0
    assert result.json["published_endpoints"] == {}


@pytest.mark.parametrize(
    "catalog_value",
    (
        accepted_catalog(matching_maturity_fingerprint=False),
        accepted_catalog(manifest_digest=False),
        accepted_catalog(profile_evidence=False),
    ),
    ids=("stale-maturity-hash", "missing-manifest", "missing-profile-evidence"),
)
def test_status_requires_complete_current_acceptance_evidence(
    catalog_value: Catalog,
) -> None:
    result = invoke(
        "status",
        "--json",
        state=active_state(catalog_value),
        catalog_value=catalog_value,
    )

    assert result.exit_code == 0
    assert result.json["published_endpoints"] == {}


def test_endpoint_allows_exact_currently_accepted_content() -> None:
    catalog_value = accepted_catalog()

    result = invoke(
        "endpoint",
        "deepseek",
        "--json",
        state=active_state(catalog_value),
        catalog_value=catalog_value,
    )

    assert result.exit_code == 0
    assert result.json["available"] is True
    assert result.json["workload_id"] == "deepseek-agent-dual"


def test_unknown_selector_is_a_configuration_error() -> None:
    result = invoke("validate", "does-not-exist", "--json")

    assert result.exit_code == 2
    assert result.json == {
        "error": "unknown cluster profile or selector: does-not-exist",
        "error_type": "configuration",
    }


def test_transition_failure_is_exit_six_and_redacts_bounded_errors() -> None:
    class FailedSwitcher(FakeSwitcher):
        def switch_profile(self, target_id, *, restore_to=None, dry_run=False):
            return SwitchReport(
                target_profile=target_id,
                status="stopped",
                profile_sha256="a" * 64,
                definition_sha256={},
                published_endpoints={},
                errors=("Authorization: Bearer supersecret " + "x" * 5_000,),
            )

    result = invoke("switch", "default", "--json", switcher=FailedSwitcher())

    assert result.exit_code == 6
    assert "supersecret" not in result.stdout
    assert "<redacted>" in result.stdout
    assert len(result.stdout) < 2_000


def test_human_status_is_readable_and_not_json() -> None:
    result = invoke("status")

    assert result.exit_code == 0
    assert result.stdout.startswith("status: stopped\n")
    assert not result.stdout.startswith("{")


def test_argument_errors_use_exit_two_and_json_when_requested() -> None:
    result = invoke("--json", "switch")

    assert result.exit_code == 2
    assert result.json["error_type"] == "arguments"
    assert "selector" in result.json["error"]


@pytest.mark.parametrize(
    ("option", "secret_value"),
    (
        ("--token", "token-value-123"),
        ("--api-key", "key-value-456"),
        ("--password", "password-value-789"),
        ("--authorization", "Bearer bearer-value-321"),
    ),
)
def test_argument_errors_never_echo_whitespace_separated_secrets(
    option: str, secret_value: str
) -> None:
    result = invoke("--json", "status", option, secret_value)

    assert result.exit_code == 2
    assert secret_value not in result.stdout
    assert result.json == {
        "error": "invalid command arguments",
        "error_type": "arguments",
    }


def test_default_dependencies_use_local_state_and_conservative_inventory(
    tmp_path: Path,
) -> None:
    dependencies = build_dependencies(
        REPOSITORY_ROOT, state_directory=tmp_path / "sparkctl"
    )

    assert dependencies.state_store.load().status == "stopped"
    assert dependencies.inventory_provider() == {"spark1": {}, "spark2": {}}
    assert not (tmp_path / "sparkctl").exists()


def test_bin_script_finds_the_repository_when_run_elsewhere(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, REPOSITORY_ROOT / "bin/sparkctl", "status", "--json"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "stopped"

    invalid = subprocess.run(
        [sys.executable, REPOSITORY_ROOT / "bin/sparkctl", "--json", "switch"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert invalid.returncode == 2
    assert json.loads(invalid.stdout)["error_type"] == "arguments"


def test_switch_rejects_unknown_selector_before_the_switcher() -> None:
    result = invoke("switch", "missing", "--json")

    assert result.exit_code == 2
    assert result.json == {
        "error": "unknown cluster profile or selector: missing",
        "error_type": "configuration",
    }


def test_active_profile_refuses_an_unpublished_endpoint() -> None:
    catalog_value = accepted_catalog()

    result = invoke(
        "endpoint",
        "unknown",
        "--json",
        state=active_state(catalog_value),
        catalog_value=catalog_value,
    )

    assert result.exit_code == 3
    assert result.json == {
        "available": False,
        "endpoint": "unknown",
        "reason": "endpoint is not published by active profile agent-full-dual",
    }


def test_endpoint_refuses_stale_active_fingerprints() -> None:
    catalog = Catalog.load(REPOSITORY_ROOT)
    state = ControllerState(
        status="active",
        active_profile="agent-full-dual",
        target_profile=None,
        restore_profile=None,
        last_error=None,
        active_profile_sha256="f" * 64,
        active_definition_sha256={
            "deepseek-agent-dual": catalog.definition_fingerprints[
                "deepseek-agent-dual"
            ]
        },
    )

    result = invoke("endpoint", "deepseek", "--json", state=state)

    assert result.exit_code == 3
    assert result.json == {
        "available": False,
        "endpoint": "deepseek",
        "reason": "active controller fingerprints do not match the catalog",
    }


def test_switch_lock_conflict_is_exit_seven() -> None:
    class LockedSwitcher(FakeSwitcher):
        def switch_profile(self, target_id, *, restore_to=None, dry_run=False):
            raise LockBusy("switch lock is held")

    result = invoke("switch", "default", "--json", switcher=LockedSwitcher())

    assert result.exit_code == 7
    assert result.json == {
        "error": "switch lock is held",
        "error_type": "lock_conflict",
    }


def test_malformed_local_state_is_a_bounded_configuration_error() -> None:
    class MalformedStore(FakeStore):
        def load(self):
            raise StateFormatError("state " + "x" * 5_000)

    result = invoke("status", "--json", store=MalformedStore())

    assert result.exit_code == 2
    assert result.json["error_type"] == "configuration"
    assert len(result.json["error"]) <= 1_024


@pytest.mark.parametrize(
    "argv", (("status", "--json"), ("endpoint", "deepseek", "--json"))
)
def test_state_load_oserror_is_a_bounded_configuration_error(
    argv: tuple[str, ...],
) -> None:
    class UnreadableStore(FakeStore):
        def load(self):
            raise OSError("local state read failed " + "x" * 5_000)

    result = invoke(*argv, store=UnreadableStore())

    assert result.exit_code == 2
    assert result.json["error_type"] == "configuration"
    assert len(result.json["error"]) <= 1_024


def test_stale_lock_oserror_is_a_bounded_configuration_error() -> None:
    result = invoke(
        "break-stale-lock",
        "--json",
        store=FakeStore(stale_error=OSError("local lock read failed")),
    )

    assert result.exit_code == 2
    assert result.json == {
        "error": "local lock read failed",
        "error_type": "configuration",
    }


def test_switch_oserror_is_configuration_exit_two_not_a_traceback() -> None:
    class UnreadableSwitcher(FakeSwitcher):
        def switch_profile(self, target_id, *, restore_to=None, dry_run=False):
            raise OSError("local state write failed")

    result = invoke("switch", "default", "--json", switcher=UnreadableSwitcher())

    assert result.exit_code == 2
    assert result.json == {
        "error": "local state write failed",
        "error_type": "configuration",
    }
