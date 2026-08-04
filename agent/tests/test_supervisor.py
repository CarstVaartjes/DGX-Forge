from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
SUPERVISOR = PROJECT / "supervisor" / "dgx-agent-supervisor"
AGENT_UNIT = PROJECT / "systemd" / "dgx-forge-agent.service"
SUPERVISOR_UNIT = PROJECT / "systemd" / "dgx-forge-agent-supervisor.service"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SupervisorHost:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.actions = root / "systemctl-actions"
        self.actions.write_text("")
        self.systemctl = root / "systemctl"
        self.systemctl.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "$DGX_TEST_SYSTEMCTL_ACTIONS"\n'
            'if [ "${1:-}" = is-failed ]; then exit 1; fi\n'
            "exit 0\n"
        )
        self.systemctl.chmod(0o755)
        self.environment = {
            **os.environ,
            "DGX_SUPERVISOR_TEST_ROOT": str(root / "host"),
            "DGX_SUPERVISOR_SYSTEMCTL": str(self.systemctl),
            "DGX_TEST_SYSTEMCTL_ACTIONS": str(self.actions),
            "DGX_SUPERVISOR_TEST_UID": str(os.geteuid()),
            "DGX_SUPERVISOR_POLL_SECONDS": "0.01",
        }

    @property
    def host_root(self) -> Path:
        return Path(self.environment["DGX_SUPERVISOR_TEST_ROOT"])

    @property
    def state_path(self) -> Path:
        return self.host_root / "var/lib/dgx-forge-agent-supervisor/state.json"

    @property
    def readiness_path(self) -> Path:
        return self.host_root / "run/dgx-forge-agent/readiness.json"

    def compile_agent(self, slot: str, message: str) -> Path:
        source = self.root / f"agent-{slot}.c"
        source.write_text(
            "#include <stdio.h>\n"
            'int main(void) { puts("' + message + '"); return 0; }\n'
        )
        target = self.host_root / "opt/dgx-forge/agent-slots" / slot / "dgx-forge-agent"
        target.parent.mkdir(parents=True, mode=0o755)
        subprocess.run(
            ["cc", "-O2", "-o", str(target), str(source)],
            check=True,
            capture_output=True,
        )
        target.chmod(0o555)
        return target

    def compile_readiness_agent(self, slot: str) -> Path:
        source = self.root / f"agent-ready-{slot}.c"
        source.write_text(
            "#include <fcntl.h>\n#include <stdio.h>\n#include <stdlib.h>\n"
            "#include <sys/stat.h>\n#include <unistd.h>\n"
            "int main(void) {\n"
            f'FILE *f=fopen("{self.readiness_path}", "w"); if (!f) return 2;\n'
            'fprintf(f, "{\\"generation\\":%s,\\"schema_version\\":1,'
            '\\"sha256\\":\\"%s\\",\\"slot\\":\\"%s\\"}\\n", '
            'getenv("DGX_AGENT_SUPERVISOR_GENERATION"), '
            'getenv("DGX_AGENT_SUPERVISOR_SHA256"), '
            'getenv("DGX_AGENT_SUPERVISOR_SLOT"));\n'
            "fflush(f); fsync(fileno(f)); fchmod(fileno(f), 0600); fclose(f); return 0; }\n"
        )
        target = self.host_root / "opt/dgx-forge/agent-slots" / slot / "dgx-forge-agent"
        target.parent.mkdir(parents=True, mode=0o755)
        subprocess.run(
            ["cc", "-O2", "-o", str(target), str(source)],
            check=True,
            capture_output=True,
        )
        target.chmod(0o555)
        return target

    def spawn_agent_from_systemctl(self) -> None:
        self.systemctl.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "$DGX_TEST_SYSTEMCTL_ACTIONS"\n'
            'if [ "$1 $2" = "restart dgx-forge-agent.service" ]; then\n'
            '  "$DGX_TEST_SUPERVISOR" run-agent &\n'
            "fi\n"
            'if [ "${1:-}" = is-failed ]; then exit 1; fi\n'
            "exit 0\n"
        )
        self.systemctl.chmod(0o755)
        self.environment["DGX_TEST_SUPERVISOR"] = str(SUPERVISOR)

    def run(
        self, *arguments: str, timeout: float = 5
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SUPERVISOR), *arguments],
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def run_with_umask(
        self, umask: int, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SUPERVISOR), *arguments],
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            preexec_fn=lambda: os.umask(umask),
        )

    def state(self) -> dict[str, object]:
        return json.loads(self.state_path.read_text())

    def readiness(self, *, generation: int, slot: str, digest: str) -> None:
        self.readiness_path.parent.mkdir(parents=True, exist_ok=True)
        self.readiness_path.parent.chmod(0o700)
        document = {
            "generation": generation,
            "schema_version": 1,
            "sha256": digest,
            "slot": slot,
        }
        self.readiness_path.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        )
        self.readiness_path.chmod(0o600)


@pytest.fixture
def supervisor_host(tmp_path: Path) -> SupervisorHost:
    return SupervisorHost(tmp_path)


def test_initialize_and_run_agent_executes_only_verified_elf(
    supervisor_host: SupervisorHost,
) -> None:
    agent = supervisor_host.compile_agent("A", "slot-a")

    initialized = supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(agent)
    )
    launched = supervisor_host.run("run-agent")

    assert initialized.returncode == 0, initialized.stderr
    assert launched.returncode == 0, launched.stderr
    assert launched.stdout == "slot-a\n"
    assert supervisor_host.state() == {
        "activation_deadline": None,
        "active_slot": "A",
        "boot_attempts": 0,
        "expected_sha256": _digest(agent),
        "generation": 1,
        "previous_slot": None,
        "rollback_performed": False,
        "schema_version": 1,
        "slot_sha256": {"A": _digest(agent), "B": None},
        "status": "stable",
    }


def test_run_agent_rejects_script_and_hardlinked_or_tampered_elf(
    supervisor_host: SupervisorHost,
) -> None:
    agent = supervisor_host.compile_agent("A", "slot-a")
    digest = _digest(agent)
    assert (
        supervisor_host.run("initialize", "--slot", "A", "--sha256", digest).returncode
        == 0
    )

    link = agent.with_name("other-link")
    os.link(agent, link)
    hardlinked = supervisor_host.run("run-agent")
    link.unlink()
    agent.chmod(0o755)
    agent.write_bytes(b"#!/usr/bin/python3\nprint('mutable import')\n")
    agent.chmod(0o555)
    script = supervisor_host.run("run-agent")

    assert hardlinked.returncode != 0
    assert script.returncode != 0
    assert hardlinked.stdout == script.stdout == ""


def test_slot_path_rejects_symlink_in_any_ancestor(
    supervisor_host: SupervisorHost,
) -> None:
    agent = supervisor_host.compile_agent("A", "slot-a")
    digest = _digest(agent)
    dgx_root = supervisor_host.host_root / "opt/dgx-forge"
    moved = supervisor_host.root / "moved-dgx-forge"
    dgx_root.rename(moved)
    dgx_root.symlink_to(moved, target_is_directory=True)

    initialized = supervisor_host.run("initialize", "--slot", "A", "--sha256", digest)

    assert initialized.returncode != 0


def test_state_publication_is_exact_mode_even_under_service_umask(
    supervisor_host: SupervisorHost,
) -> None:
    agent = supervisor_host.compile_agent("A", "slot-a")

    initialized = supervisor_host.run_with_umask(
        0o077, "initialize", "--slot", "A", "--sha256", _digest(agent)
    )

    assert initialized.returncode == 0, initialized.stderr
    assert supervisor_host.state_path.stat().st_mode & 0o777 == 0o644
    assert supervisor_host.run("run-agent").returncode == 0


def test_stable_supervisor_prepares_clean_boot_runtime_without_dependency_cycle(
    supervisor_host: SupervisorHost,
) -> None:
    agent = supervisor_host.compile_agent("A", "slot-a")
    assert (
        supervisor_host.run(
            "initialize", "--slot", "A", "--sha256", _digest(agent)
        ).returncode
        == 0
    )
    assert not supervisor_host.readiness_path.parent.exists()

    supervised = supervisor_host.run("supervise")

    assert supervised.returncode == 0, supervised.stderr
    runtime = supervisor_host.readiness_path.parent
    assert runtime.is_dir()
    assert runtime.stat().st_mode & 0o777 == 0o700


def test_concurrent_initialization_serializes_and_publication_crashes_recover(
    tmp_path: Path,
) -> None:
    concurrent = SupervisorHost(tmp_path / "concurrent")
    agent = concurrent.compile_agent("A", "slot-a")
    arguments = ("initialize", "--slot", "A", "--sha256", _digest(agent))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: concurrent.run(*arguments), range(4)))
    assert [result.returncode for result in results] == [0, 0, 0, 0]

    for stage in ("create", "write", "file-fsync", "rename", "directory-fsync"):
        host = SupervisorHost(tmp_path / stage)
        artifact = host.compile_agent("A", "slot-a")
        host.environment["DGX_SUPERVISOR_CRASH_AFTER"] = stage
        crashed = host.run("initialize", "--slot", "A", "--sha256", _digest(artifact))
        assert crashed.returncode == 99
        host.environment.pop("DGX_SUPERVISOR_CRASH_AFTER")

        recovered = host.run("initialize", "--slot", "A", "--sha256", _digest(artifact))

        assert recovered.returncode == 0, recovered.stderr
        state_root = host.state_path.parent
        assert not list(state_root.glob(".state.*.new"))
        assert host.run("run-agent").returncode == 0


def test_activation_accepts_only_exact_generation_bound_readiness(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_agent("A", "slot-a")
    b = supervisor_host.compile_agent("B", "slot-b")
    digest_a, digest_b = _digest(a), _digest(b)
    assert (
        supervisor_host.run(
            "initialize", "--slot", "A", "--sha256", digest_a
        ).returncode
        == 0
    )
    activated = supervisor_host.run("activate", "--slot", "B", "--sha256", digest_b)
    assert activated.returncode == 0, activated.stderr
    assert (
        "--no-block restart dgx-forge-agent-supervisor.service"
        in supervisor_host.actions.read_text().splitlines()
    )
    generation = int(supervisor_host.state()["generation"])
    supervisor_host.readiness(generation=generation - 1, slot="B", digest=digest_b)
    supervisor_host.systemctl.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$DGX_TEST_SYSTEMCTL_ACTIONS"\n'
        'if [ "${1:-}" = is-failed ]; then exit 0; fi\n'
        "exit 0\n"
    )
    supervisor_host.systemctl.chmod(0o755)
    supervised = supervisor_host.run("supervise")

    assert supervised.returncode != 0
    state = supervisor_host.state()
    assert state["active_slot"] == "A"
    assert state["expected_sha256"] == digest_a
    assert state["rollback_performed"] is True
    assert not supervisor_host.readiness_path.exists()


def test_activation_with_exact_readiness_commits_new_slot(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_agent("A", "slot-a")
    b = supervisor_host.compile_agent("B", "slot-b")
    assert (
        supervisor_host.run(
            "initialize", "--slot", "A", "--sha256", _digest(a)
        ).returncode
        == 0
    )
    assert (
        supervisor_host.run(
            "activate", "--slot", "B", "--sha256", _digest(b)
        ).returncode
        == 0
    )
    pending = supervisor_host.state()
    supervisor_host.readiness(
        generation=int(pending["generation"]), slot="B", digest=_digest(b)
    )

    supervised = supervisor_host.run("supervise")

    assert supervised.returncode == 0, supervised.stderr
    state = supervisor_host.state()
    assert state["active_slot"] == "B"
    assert state["previous_slot"] == "A"
    assert state["status"] == "stable"
    assert state["activation_deadline"] is None


def test_readiness_replacement_during_consumption_is_not_unlinked(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_agent("A", "slot-a")
    b = supervisor_host.compile_agent("B", "slot-b")
    assert (
        supervisor_host.run(
            "initialize", "--slot", "A", "--sha256", _digest(a)
        ).returncode
        == 0
    )
    assert (
        supervisor_host.run(
            "activate", "--slot", "B", "--sha256", _digest(b)
        ).returncode
        == 0
    )
    pending = supervisor_host.state()
    generation = int(pending["generation"])
    supervisor_host.readiness(generation=generation, slot="B", digest=_digest(b))
    replacement = supervisor_host.readiness_path.with_name("replacement.json")
    replacement.write_text(
        json.dumps(
            {
                "generation": generation - 1,
                "schema_version": 1,
                "sha256": _digest(b),
                "slot": "B",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    replacement.chmod(0o600)
    supervisor_host.environment["DGX_SUPERVISOR_SWAP_READINESS_TEST"] = "1"

    supervised = supervisor_host.run("supervise")

    assert supervised.returncode == 0, supervised.stderr
    assert supervisor_host.state()["status"] == "stable"
    assert json.loads(supervisor_host.readiness_path.read_text())["generation"] == (
        generation - 1
    )


def test_supervise_releases_writer_lock_so_restarted_agent_can_emit_readiness(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_agent("A", "slot-a")
    b = supervisor_host.compile_readiness_agent("B")
    assert (
        supervisor_host.run(
            "initialize", "--slot", "A", "--sha256", _digest(a)
        ).returncode
        == 0
    )
    assert (
        supervisor_host.run(
            "activate", "--slot", "B", "--sha256", _digest(b)
        ).returncode
        == 0
    )
    supervisor_host.readiness_path.parent.mkdir(parents=True, exist_ok=True)
    supervisor_host.readiness_path.parent.chmod(0o700)
    supervisor_host.spawn_agent_from_systemctl()

    supervised = supervisor_host.run("supervise", timeout=3)

    assert supervised.returncode == 0, supervised.stderr
    assert supervisor_host.state()["status"] == "stable"
    assert not supervisor_host.readiness_path.exists()


def test_pending_invalid_active_slot_rolls_back_to_verified_previous(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_agent("A", "slot-a")
    b = supervisor_host.compile_agent("B", "slot-b")
    digest_a, digest_b = _digest(a), _digest(b)
    assert (
        supervisor_host.run(
            "initialize", "--slot", "A", "--sha256", digest_a
        ).returncode
        == 0
    )
    assert (
        supervisor_host.run("activate", "--slot", "B", "--sha256", digest_b).returncode
        == 0
    )
    b.unlink()

    supervised = supervisor_host.run("supervise")

    assert supervised.returncode != 0
    state = supervisor_host.state()
    assert state["active_slot"] == "A"
    assert state["expected_sha256"] == digest_a
    assert state["rollback_performed"] is True


def test_corrupt_state_and_both_invalid_slots_fail_closed(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_agent("A", "slot-a")
    assert (
        supervisor_host.run(
            "initialize", "--slot", "A", "--sha256", _digest(a)
        ).returncode
        == 0
    )
    supervisor_host.state_path.write_text('{"schema_version":1,"schema_version":1}\n')

    corrupt = supervisor_host.run("run-agent")

    assert corrupt.returncode != 0
    assert corrupt.stdout == ""


def test_nonfinite_activation_deadline_fails_closed(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_agent("A", "slot-a")
    b = supervisor_host.compile_agent("B", "slot-b")
    assert (
        supervisor_host.run(
            "initialize", "--slot", "A", "--sha256", _digest(a)
        ).returncode
        == 0
    )
    assert (
        supervisor_host.run(
            "activate", "--slot", "B", "--sha256", _digest(b)
        ).returncode
        == 0
    )
    state = supervisor_host.state()
    state["activation_deadline"] = float("nan")
    supervisor_host.state_path.write_text(
        json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n"
    )

    launched = supervisor_host.run("run-agent")

    assert launched.returncode != 0
    assert launched.stdout == ""


def test_state_generation_matches_readiness_reporter_bound(
    supervisor_host: SupervisorHost,
) -> None:
    agent = supervisor_host.compile_agent("A", "slot-a")
    assert (
        supervisor_host.run(
            "initialize", "--slot", "A", "--sha256", _digest(agent)
        ).returncode
        == 0
    )
    state = supervisor_host.state()
    state["generation"] = 1_000_000_000
    supervisor_host.state_path.write_text(
        json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n"
    )

    rejected = supervisor_host.run("run-agent")

    assert rejected.returncode != 0


def test_supervisor_interface_has_no_path_or_shell_argument(
    supervisor_host: SupervisorHost,
) -> None:
    result = supervisor_host.run(
        "activate", "--slot", "B", "--sha256", "a" * 64, "--path", "/tmp/x"
    )
    shell = supervisor_host.run("/bin/sh", "-c", "id")

    assert result.returncode == shell.returncode == 64


def test_arm64_elf_is_validated_without_execution(
    supervisor_host: SupervisorHost,
) -> None:
    if platform.machine() not in {"x86_64", "AMD64"}:
        pytest.skip("foreign ARM64 validation is exercised from x86_64")
    target = supervisor_host.host_root / "opt/dgx-forge/agent-slots/A/dgx-forge-agent"
    target.parent.mkdir(parents=True)
    # ELF64 little-endian, ET_EXEC, EM_AARCH64; no executable body is needed.
    target.write_bytes(
        b"\x7fELF\x02\x01\x01" + b"\0" * 9 + b"\x02\0\xb7\0" + b"\0" * 44
    )
    target.chmod(0o555)
    supervisor_host.environment["DGX_SUPERVISOR_TEST_ARCH"] = "aarch64"

    initialized = supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(target)
    )

    assert initialized.returncode == 0, initialized.stderr


def test_systemd_units_verify_and_enforce_split_privilege_hardening(
    tmp_path: Path,
) -> None:
    unit_root = tmp_path / "unit-root"
    unit_directory = unit_root / "etc/systemd/system"
    executable_directory = unit_root / "usr/libexec"
    shutil.copytree("/usr/lib/systemd/system", unit_root / "usr/lib/systemd/system")
    unit_directory.mkdir(parents=True)
    executable_directory.mkdir(parents=True)
    for source in (AGENT_UNIT, SUPERVISOR_UNIT):
        shutil.copy2(source, unit_directory / source.name)
    shutil.copy2("/bin/true", executable_directory / "dgx-agent-supervisor")
    verified = subprocess.run(
        [
            "systemd-analyze",
            "verify",
            f"--root={unit_root}",
            str(unit_directory / AGENT_UNIT.name),
            str(unit_directory / SUPERVISOR_UNIT.name),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    effective: dict[str, dict[str, object]] = {}
    for unit in (AGENT_UNIT, SUPERVISOR_UNIT):
        analyzed = subprocess.run(
            [
                "systemd-analyze",
                "security",
                "--offline=yes",
                "--json=short",
                str(unit),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        assert analyzed.returncode == 0, analyzed.stderr
        effective[unit.name] = {
            assessment["json_field"]: assessment["set"]
            for assessment in json.loads(analyzed.stdout)
        }
    assert effective[AGENT_UNIT.name]["UserOrDynamicUser"] is True
    assert effective[AGENT_UNIT.name]["NoNewPrivileges"] is True
    assert effective[AGENT_UNIT.name]["ProtectSystem"] is True
    assert effective[SUPERVISOR_UNIT.name]["PrivateNetwork"] is True
    assert effective[SUPERVISOR_UNIT.name]["NoNewPrivileges"] is True
    assert effective[SUPERVISOR_UNIT.name]["ProtectSystem"] is True
    agent = AGENT_UNIT.read_text()
    supervisor = SUPERVISOR_UNIT.read_text()
    for literal in (
        "User=dgx-agent",
        "Group=dgx-agent",
        "SupplementaryGroups=",
        "PartOf=dgx-forge-agent-supervisor.service",
        "ExecStart=/usr/libexec/dgx-agent-supervisor run-agent",
        "UMask=0077",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "PrivateTmp=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "MemoryDenyWriteExecute=yes",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "ReadWritePaths=/var/lib/dgx-forge-agent /var/lib/dgx-forge/releases /var/lib/dgx-forge/release-staging /run/dgx-forge-agent",
    ):
        assert literal in agent
    assert "docker" not in agent.lower()
    for literal in (
        "ExecStart=/usr/libexec/dgx-agent-supervisor supervise",
        "UMask=0077",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=CAP_DAC_READ_SEARCH CAP_DAC_OVERRIDE",
        "AmbientCapabilities=",
        "PrivateNetwork=yes",
        "ProtectSystem=strict",
    ):
        assert literal in supervisor
    assert "User=" not in supervisor
