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
ACTIVATION_UNIT = PROJECT / "systemd" / "dgx-forge-agent-activation.service"
ACTIVATION_PATH = PROJECT / "systemd" / "dgx-forge-agent-activation.path"
SYSTEMD_VERIFY = PROJECT.parent / "scripts" / "verify-agent-systemd"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_supervisor_entrypoint_ignores_writable_python_site_hooks(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "sitecustomize-ran"
    python_path = tmp_path / "python-path"
    python_path.mkdir()
    (python_path / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('unsafe')\n"
    )
    home = tmp_path / "home"
    user_site = home / (
        f".local/lib/python{platform.python_version_tuple()[0]}."
        f"{platform.python_version_tuple()[1]}/site-packages"
    )
    user_site.mkdir(parents=True)
    (user_site / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('unsafe')\n"
    )

    result = subprocess.run(
        [str(SUPERVISOR), "--help"],
        env={**os.environ, "HOME": str(home), "PYTHONPATH": str(python_path)},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()


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

    @property
    def activation_request_path(self) -> Path:
        return self.host_root / "run/dgx-forge-agent/activation-request.json"

    def stage_update_request(
        self, candidate: Path, *, previous: str = "A", target: str = "B"
    ) -> str:
        digest = _digest(candidate)
        staging = self.host_root / "var/lib/dgx-forge-agent/update-staging"
        staging.mkdir(parents=True, mode=0o700)
        staged = staging / f"{digest}.agent"
        shutil.copyfile(candidate, staged)
        staged.chmod(0o500)
        self.activation_request_path.parent.mkdir(parents=True, exist_ok=True)
        self.activation_request_path.parent.chmod(0o700)
        request = {
            "build_digest": "sha256:" + digest,
            "platform_version": "1.2.0",
            "previous_slot": previous,
            "schema_version": 1,
            "sha256": digest,
            "size": staged.stat().st_size,
            "target_slot": target,
        }
        self.activation_request_path.write_text(
            json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n"
        )
        self.activation_request_path.chmod(0o600)
        return digest

    @staticmethod
    def write_identity(target: Path, *, platform_version: str = "1.0.0") -> None:
        digest = _digest(target)
        target.with_name("identity.json").write_text(
            json.dumps(
                {
                    "build_digest": "sha256:" + digest,
                    "platform_version": platform_version,
                    "schema_version": 1,
                    "sha256": digest,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        target.with_name("identity.json").chmod(0o444)

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
        self.write_identity(target)
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
        self.write_identity(target)
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


def test_run_agent_exports_verified_release_identity(
    supervisor_host: SupervisorHost,
) -> None:
    source = supervisor_host.root / "identity-agent.c"
    source.write_text(
        "#include <stdio.h>\n#include <stdlib.h>\n"
        "int main(void){printf(\"%s %s\\n\",getenv(\"DGX_AGENT_PLATFORM_VERSION\"),"
        "getenv(\"DGX_AGENT_BUILD_DIGEST\"));}\n"
    )
    target = (
        supervisor_host.host_root
        / "opt/dgx-forge/agent-slots/A/dgx-forge-agent"
    )
    target.parent.mkdir(parents=True)
    subprocess.run(["cc", "-O2", "-o", target, source], check=True)
    target.chmod(0o555)
    supervisor_host.write_identity(target, platform_version="7.8.9")
    digest = _digest(target)

    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", digest
    ).returncode == 0
    launched = supervisor_host.run("run-agent")

    assert launched.returncode == 0, launched.stderr
    assert launched.stdout == f"7.8.9 sha256:{digest}\n"


def test_apply_request_installs_verified_inactive_slot_and_starts_activation(
    supervisor_host: SupervisorHost,
) -> None:
    active = supervisor_host.compile_agent("A", "slot-a")
    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(active)
    ).returncode == 0
    candidate = supervisor_host.root / "candidate"
    source = supervisor_host.root / "candidate.c"
    source.write_text('#include <stdio.h>\nint main(void){puts("slot-b");}\n')
    subprocess.run(["cc", "-O2", "-o", candidate, source], check=True)
    digest = supervisor_host.stage_update_request(candidate)

    applied = supervisor_host.run("apply-request")

    assert applied.returncode == 0, applied.stderr
    installed = (
        supervisor_host.host_root
        / "opt/dgx-forge/agent-slots/B/dgx-forge-agent"
    )
    assert _digest(installed) == digest
    assert installed.stat().st_mode & 0o777 == 0o555
    identity = json.loads(installed.with_name("identity.json").read_text())
    assert identity == {
        "build_digest": "sha256:" + digest,
        "platform_version": "1.2.0",
        "schema_version": 1,
        "sha256": digest,
    }
    assert not supervisor_host.activation_request_path.exists()
    state = supervisor_host.state()
    assert state["active_slot"] == "B"
    assert state["previous_slot"] == "A"
    assert state["status"] == "pending"
    assert "--no-block restart dgx-forge-agent-supervisor.service" in (
        supervisor_host.actions.read_text()
    )


@pytest.mark.parametrize(
    ("previous", "target"),
    [("B", "B"), ("A", "A")],
)
def test_apply_request_rejects_wrong_previous_or_active_target(
    supervisor_host: SupervisorHost, previous: str, target: str
) -> None:
    active = supervisor_host.compile_agent("A", "slot-a")
    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(active)
    ).returncode == 0
    candidate = supervisor_host.compile_agent("B", "slot-b")
    supervisor_host.stage_update_request(candidate, previous=previous, target=target)

    applied = supervisor_host.run("apply-request")

    assert applied.returncode == 1
    assert supervisor_host.state()["active_slot"] == "A"


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


def test_pending_slot_replacement_before_commit_rolls_back(
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
    replacement = b.with_name(".dgx-forge-agent.commit-race")
    replacement.write_bytes(a.read_bytes())
    replacement.chmod(0o555)
    supervisor_host.environment["DGX_SUPERVISOR_SWAP_SLOT_BEFORE_COMMIT_TEST"] = "1"

    supervised = supervisor_host.run("supervise")

    assert supervised.returncode != 0
    state = supervisor_host.state()
    assert state["active_slot"] == "A"
    assert state["expected_sha256"] == _digest(a)
    assert state["rollback_performed"] is True


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


def test_readiness_replacement_after_identity_check_survives(
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
    supervisor_host.environment[
        "DGX_SUPERVISOR_SWAP_READINESS_AFTER_STAT_TEST"
    ] = "1"

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
    supervisor_host.write_identity(target)
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
    units = (AGENT_UNIT, SUPERVISOR_UNIT, ACTIVATION_UNIT, ACTIVATION_PATH)
    for source in units:
        shutil.copy2(source, unit_directory / source.name)
    shutil.copy2("/bin/true", executable_directory / "dgx-agent-supervisor")
    verified = subprocess.run(
        [
            "systemd-analyze",
            "verify",
            f"--root={unit_root}",
            *(str(unit_directory / unit.name) for unit in units),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    effective: dict[str, dict[str, object]] = {}
    for unit in (AGENT_UNIT, SUPERVISOR_UNIT, ACTIVATION_UNIT):
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
    assert effective[ACTIVATION_UNIT.name]["PrivateNetwork"] is True
    assert effective[ACTIVATION_UNIT.name]["NoNewPrivileges"] is True
    assert effective[ACTIVATION_UNIT.name]["ProtectSystem"] is True
    assert (
        effective[SUPERVISOR_UNIT.name][
            "CapabilityBoundingSet_CAP_CHOWN_FSETID_SETFCAP"
        ]
        is False
    )
    agent = AGENT_UNIT.read_text()
    supervisor = SUPERVISOR_UNIT.read_text()
    activation = ACTIVATION_UNIT.read_text()
    activation_path = ACTIVATION_PATH.read_text()
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
        "CapabilityBoundingSet=CAP_CHOWN CAP_DAC_READ_SEARCH CAP_DAC_OVERRIDE",
        "AmbientCapabilities=",
        "PrivateNetwork=yes",
        "ProtectSystem=strict",
    ):
        assert literal in supervisor
    assert "User=" not in supervisor
    for literal in (
        "ExecStart=/usr/libexec/dgx-agent-supervisor apply-request",
        "NoNewPrivileges=yes",
        "PrivateNetwork=yes",
        "ProtectSystem=strict",
        "ReadOnlyPaths=/var/lib/dgx-forge-agent/update-staging /usr/libexec/dgx-agent-supervisor",
        "ReadWritePaths=/opt/dgx-forge/agent-slots /var/lib/dgx-forge-agent-supervisor /run/dgx-forge-agent",
    ):
        assert literal in activation
    assert "User=" not in activation
    assert "PathExists=/run/dgx-forge-agent/activation-request.json" in activation_path
    assert "Unit=dgx-forge-agent-activation.service" in activation_path


def test_agent_effective_device_policy_is_closed_and_read_only() -> None:
    analyzed = subprocess.run(
        [
            "systemd-analyze",
            "security",
            "--offline=yes",
            "--json=short",
            str(AGENT_UNIT),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert analyzed.returncode == 0, analyzed.stderr
    assessments = {
        assessment["json_field"]: assessment
        for assessment in json.loads(analyzed.stdout)
    }
    assert assessments["PrivateDevices"]["set"] is True
    device_acl = assessments["DeviceAllow"]["description"].split(": ", 1)[1]
    assert set(device_acl.split()) == {
        "/dev/nvidia-caps/nvidia-cap2:r",
        "/dev/nvidia-modeset:r",
        "/dev/nvidia-uvm-tools:r",
        "/dev/nvidia-uvm:r",
        "/dev/nvidia0:r",
        "/dev/nvidiactl:r",
        "char-rtc:r",
    }
    directives = AGENT_UNIT.read_text().splitlines()
    assert "DevicePolicy=closed" in directives
    bind = next(
        line.removeprefix("BindReadOnlyPaths=").split()
        for line in directives
        if line.startswith("BindReadOnlyPaths=")
    )
    assert set(bind) == {
        "-/dev/nvidia-caps/nvidia-cap2",
        "-/dev/nvidia-modeset",
        "-/dev/nvidia-uvm-tools",
        "-/dev/nvidia-uvm",
        "-/dev/nvidia0",
        "-/dev/nvidiactl",
    }


def test_installed_systemd_harness_verifies_units_by_installed_name() -> None:
    assert SYSTEMD_VERIFY.is_file()

    verified = subprocess.run(
        [str(SYSTEMD_VERIFY), "--json"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert verified.returncode == 0, verified.stderr
    report = json.loads(verified.stdout)
    assert report["verify"] == "passed"
    assert set(report["units"]) == {
        "dgx-forge-agent.service",
        "dgx-forge-agent-supervisor.service",
        "dgx-forge-agent-activation.service",
        "dgx-forge-agent-activation.path",
    }
    assert set(report["security_units"]) == {
        "dgx-forge-agent.service",
        "dgx-forge-agent-supervisor.service",
        "dgx-forge-agent-activation.service",
    }
    assert all(
        unit["exposure"] == "OK" for unit in report["security_units"].values()
    )
