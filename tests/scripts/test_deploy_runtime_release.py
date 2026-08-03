from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/deploy-runtime-release"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _release_root(
    tmp_path: Path, *, nodes: tuple[object, ...] = ("spark1", "spark2")
) -> tuple[Path, str]:
    root = tmp_path / "repository"
    release = root / "adapters/example"
    (release / "bin").mkdir(parents=True)
    (release / "config").mkdir()
    (release / "bin/adapter").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (release / "bin/adapter").chmod(0o755)
    (release / "config/common.env").write_text("OFFLINE=1\n", encoding="utf-8")
    files = {
        "adapters/example/bin/adapter": _sha256(release / "bin/adapter"),
        "adapters/example/config/common.env": _sha256(
            release / "config/common.env"
        ),
    }
    manifest = release / "runtime-manifest.json"
    manifest.write_text(
        json.dumps({"files": files}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    digest = _sha256(manifest)
    workload = root / "config/workloads/example.toml"
    workload.parent.mkdir(parents=True)
    workload.write_text(
        f'''id = "example"
nodes = {json.dumps(list(nodes))}

[runtime_release]
manifest = "adapters/example/runtime-manifest.json"
sha256 = "{digest}"
''',
        encoding="utf-8",
    )
    inventory = root / "inventory/cluster.toml"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(
        '''[hosts.spark1]
ssh_alias = "dgx-spark-1"

[hosts.spark2]
ssh_alias = "dgx-spark-2"
''',
        encoding="utf-8",
    )
    return root, digest


def _run(root: Path, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _json_lines(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _fake_remote_environment(
    tmp_path: Path, *, probe: str = "absent"
) -> tuple[dict[str, str], Path, Path]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    ssh_log = tmp_path / "ssh.jsonl"
    scp_log = tmp_path / "scp.jsonl"
    ssh = fake_bin / "ssh"
    ssh.write_text(
        """#!/usr/bin/env python3
import json, os, sys
payload = sys.stdin.read()
with open(os.environ["FAKE_SSH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "stdin": payload}) + "\\n")
if "deploy-runtime-release: probe" in payload:
    result = os.environ.get("FAKE_PROBE", "absent")
    if result == "different":
        print("installed release differs", file=sys.stderr)
        raise SystemExit(43)
    print(result)
""",
        encoding="utf-8",
    )
    scp = fake_bin / "scp"
    scp.write_text(
        """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_SCP_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"argv": sys.argv[1:]}) + "\\n")
""",
        encoding="utf-8",
    )
    for command in (ssh, scp):
        command.chmod(0o755)
    env = os.environ | {
        "SPARK_SSH_BIN": str(ssh),
        "SPARK_SCP_BIN": str(scp),
        "FAKE_SSH_LOG": str(ssh_log),
        "FAKE_SCP_LOG": str(scp_log),
        "FAKE_PROBE": probe,
    }
    return env, ssh_log, scp_log


def _filesystem_remote_environment(
    tmp_path: Path,
    *,
    race_destination: bool = False,
    preserve_modes: bool = True,
) -> tuple[dict[str, str], Path]:
    fake_bin = tmp_path / "filesystem-fake-bin"
    fake_bin.mkdir()
    remote_root = tmp_path / "remote"
    for alias in ("dgx-spark-1", "dgx-spark-2"):
        (remote_root / alias / "opt/spark/model-adapters").mkdir(parents=True)

    ssh = fake_bin / "ssh"
    ssh.write_text(
        """#!/usr/bin/env python3
import os, shlex, subprocess, sys
from pathlib import Path

payload = sys.stdin.read()
alias = sys.argv[-2]
command = shlex.split(sys.argv[-1])
arguments = command[command.index("--") + 1:]
prefix = "/opt/spark/model-adapters"
base = Path(os.environ["FAKE_REMOTE_ROOT"]) / alias

def mapped(value):
    if value == prefix or value.startswith(prefix + "/"):
        return str(base / value.lstrip("/"))
    return value

mapped_arguments = [mapped(value) for value in arguments]
completed = subprocess.run(
    ["bash", "-s", "--", *mapped_arguments],
    input=payload,
    capture_output=True,
    text=True,
    check=False,
    env=os.environ | {"PATH": str(Path(sys.argv[0]).parent) + ":" + os.environ["PATH"]},
)
sys.stdout.write(completed.stdout)
sys.stderr.write(completed.stderr)
raise SystemExit(completed.returncode)
""",
        encoding="utf-8",
    )
    scp = fake_bin / "scp"
    scp.write_text(
        """#!/usr/bin/env python3
import os, shutil, sys
from pathlib import Path

source = Path(sys.argv[-2])
alias, remote_path = sys.argv[-1].split(":", 1)
destination = Path(os.environ["FAKE_REMOTE_ROOT"]) / alias / remote_path.lstrip("/")
if os.environ.get("FAKE_SCP_PRESERVE_MODES") == "1":
    shutil.copy2(source, destination)
else:
    # Model the Windows OpenSSH bridge: bytes arrive intact, but POSIX modes
    # are not preserved by scp -p across the filesystem boundary.
    shutil.copyfile(source, destination)
""",
        encoding="utf-8",
    )
    stat_command = fake_bin / "stat"
    stat_command.write_text(
        """#!/usr/bin/env python3
import os, sys
if sys.argv[1:4] != ["-c", "%a", "--"]:
    raise SystemExit(2)
print(f"{os.stat(sys.argv[4]).st_mode & 0o777:o}")
""",
        encoding="utf-8",
    )
    python_command = fake_bin / "python3"
    python_command.write_text(
        f"""#!{sys.executable}
import os, sys
if (
    len(sys.argv) >= 5
    and sys.argv[1] == "-c"
    and "rename_noreplace" in sys.argv[2].lower()
):
    from pathlib import Path
    source = Path(sys.argv[3])
    destination = Path(sys.argv[4])
    if os.environ.get("FAKE_RACE_DESTINATION") == "1":
        destination.mkdir(exist_ok=True)
    if os.path.lexists(destination):
        raise SystemExit(1)
    os.rename(source, destination)
    raise SystemExit(0)
real = os.environ["REAL_PYTHON3"]
os.execv(real, [real, *sys.argv[1:]])
""",
        encoding="utf-8",
    )
    for command in (ssh, scp, stat_command, python_command):
        command.chmod(0o755)
    environment = os.environ | {
        "SPARK_SSH_BIN": str(ssh),
        "SPARK_SCP_BIN": str(scp),
        "FAKE_REMOTE_ROOT": str(remote_root),
        "REAL_PYTHON3": sys.executable,
    }
    if race_destination:
        environment["FAKE_RACE_DESTINATION"] = "1"
    if preserve_modes:
        environment["FAKE_SCP_PRESERVE_MODES"] = "1"
    return environment, remote_root


def _replace_manifest_digest(root: Path) -> None:
    manifest = root / "adapters/example/runtime-manifest.json"
    digest = _sha256(manifest)
    workload = root / "config/workloads/example.toml"
    source = workload.read_text(encoding="utf-8")
    before = source.split('sha256 = "', 1)[1].split('"', 1)[0]
    workload.write_text(source.replace(before, digest), encoding="utf-8")


def test_default_is_a_validated_dry_run_without_remote_commands(tmp_path: Path) -> None:
    root, digest = _release_root(tmp_path)
    env = os.environ | {
        "SPARK_SSH_BIN": "/must-not-run/ssh",
        "SPARK_SCP_BIN": "/must-not-run/scp",
    }

    completed = _run(root, "example", env=env)

    assert completed.returncode == 0
    assert "dry-run" in completed.stdout
    assert "dgx-spark-1" in completed.stdout
    assert "dgx-spark-2" in completed.stdout
    assert f"/opt/spark/model-adapters/example/releases/{digest}" in completed.stdout


def test_dry_run_targets_only_the_workload_declared_node(tmp_path: Path) -> None:
    root, _ = _release_root(tmp_path, nodes=("spark1",))

    completed = _run(root, "example")

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["hosts"] == ["dgx-spark-1"]


@pytest.mark.parametrize("nodes", ((), ("spark1", "spark1"), ("spark3",)))
def test_dry_run_rejects_invalid_workload_nodes(
    tmp_path: Path, nodes: tuple[object, ...]
) -> None:
    root, _ = _release_root(tmp_path, nodes=nodes)

    completed = _run(root, "example")

    assert completed.returncode != 0
    assert "workload nodes must be a non-empty unique Spark node subset" in completed.stderr


def test_dry_run_rejects_changed_manifest_and_payload(tmp_path: Path) -> None:
    root, _ = _release_root(tmp_path)
    workload = root / "config/workloads/example.toml"
    workload.write_text(
        workload.read_text(encoding="utf-8").replace(
            'sha256 = "', 'sha256 = "0', 1
        ),
        encoding="utf-8",
    )

    manifest_result = _run(root, "example")

    assert manifest_result.returncode != 0
    assert "manifest digest" in manifest_result.stderr

    root, _ = _release_root(tmp_path / "payload")
    (root / "adapters/example/bin/adapter").write_text("changed\n", encoding="utf-8")

    payload_result = _run(root, "example")

    assert payload_result.returncode != 0
    assert "payload digest mismatch" in payload_result.stderr


def test_manifest_payloads_must_share_the_manifest_parent(tmp_path: Path) -> None:
    root, _ = _release_root(tmp_path)
    outside = root / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    manifest = root / "adapters/example/runtime-manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["files"]["outside.txt"] = _sha256(outside)
    manifest.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _replace_manifest_digest(root)

    completed = _run(root, "example")

    assert completed.returncode != 0
    assert "manifest-parent prefix" in completed.stderr


def test_local_release_rejects_a_symlinked_parent_directory(tmp_path: Path) -> None:
    root, _ = _release_root(tmp_path)
    release = root / "adapters/example"
    outside = tmp_path / "outside-config"
    shutil.move(release / "config", outside)
    (release / "config").symlink_to(outside, target_is_directory=True)

    completed = _run(root, "example")

    assert completed.returncode != 0
    assert "path must not contain symlinks" in completed.stderr


def test_workload_name_cannot_escape_the_release_namespace(tmp_path: Path) -> None:
    root, _ = _release_root(tmp_path)

    completed = _run(root, "../example")

    assert completed.returncode != 0
    assert "unsafe workload name" in completed.stderr


def test_release_modes_follow_manifest_policy_on_non_posix_checkouts(tmp_path: Path) -> None:
    root, digest = _release_root(tmp_path, nodes=("spark1",))
    adapter = root / "adapters/example/bin/adapter"
    config = root / "adapters/example/config/common.env"
    adapter.chmod(0o777)
    config.chmod(0o666)
    environment, remote_root = _filesystem_remote_environment(tmp_path)

    completed = _run(root, "--apply", "example", env=environment)

    assert completed.returncode == 0, completed.stderr
    installed = (
        remote_root
        / "dgx-spark-1/opt/spark/model-adapters/example/releases"
        / digest
    )
    assert (installed / "bin/adapter").stat().st_mode & 0o777 == 0o755
    assert (installed / "config/common.env").stat().st_mode & 0o777 == 0o644


def test_remote_install_refuses_broken_symlink_targets() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "rename_noreplace = 1" in source
    assert 'if [[ -e "$temporary" || -L "$temporary" ]]; then' in source


def test_apply_stages_verifies_and_atomically_installs_both_nodes(
    tmp_path: Path,
) -> None:
    root, digest = _release_root(tmp_path)
    env, ssh_log, scp_log = _fake_remote_environment(tmp_path)

    completed = _run(root, "--apply", "example", env=env)

    assert completed.returncode == 0, completed.stderr
    ssh_calls = _json_lines(ssh_log)
    scp_calls = _json_lines(scp_log)
    assert len(ssh_calls) == 10
    assert len(scp_calls) == 4
    for alias in ("dgx-spark-1", "dgx-spark-2"):
        calls = [call for call in ssh_calls if alias in call["argv"]]
        assert len(calls) == 5
        assert all("BatchMode=yes" in call["argv"] for call in calls)
        assert all("ForwardAgent=no" in call["argv"] for call in calls)
    finalizers = [
        call for call in ssh_calls if "deploy-runtime-release: finalize" in call["stdin"]
    ]
    assert len(finalizers) == 2
    assert all("sha256sum" in call["stdin"] for call in finalizers)
    assert all("stat -c '%a'" in call["stdin"] for call in finalizers)
    assert all(
        "rename_noreplace" in call["stdin"].lower() for call in finalizers
    )
    assert all(digest in call["argv"][-1] for call in finalizers)
    mode_calls = [
        call for call in ssh_calls if "deploy-runtime-release: mode" in call["stdin"]
    ]
    assert len(mode_calls) == 4
    assert all("chmod --" in call["stdin"] for call in mode_calls)
    remote_destinations = [call["argv"][-1] for call in scp_calls]
    assert any(destination.endswith("/bin/adapter") for destination in remote_destinations)
    assert any(
        destination.endswith("/config/common.env")
        for destination in remote_destinations
    )
    assert all("/adapters/example/" not in destination for destination in remote_destinations)
    assert all(f"/.{digest}.tmp-" in destination for destination in remote_destinations)
    transcript = json.dumps({"ssh": ssh_calls, "scp": scp_calls}).lower()
    assert "docker" not in transcript
    assert "pull" not in transcript
    assert "compose up" not in transcript


def test_apply_targets_only_the_workload_declared_node(tmp_path: Path) -> None:
    root, _ = _release_root(tmp_path, nodes=("spark1",))
    env, ssh_log, scp_log = _fake_remote_environment(tmp_path)

    completed = _run(root, "--apply", "example", env=env)

    assert completed.returncode == 0, completed.stderr
    assert len(_json_lines(ssh_log)) == 5
    assert len(_json_lines(scp_log)) == 2
    assert all("dgx-spark-1" in call["argv"] for call in _json_lines(ssh_log))
    assert all(
        call["argv"][-1].startswith("dgx-spark-1:")
        for call in _json_lines(scp_log)
    )


def test_windows_style_scp_wrapper_receives_a_windows_local_source_path(
    tmp_path: Path,
) -> None:
    root, _ = _release_root(tmp_path, nodes=("spark1",))
    env, _, scp_log = _fake_remote_environment(tmp_path)
    scp = Path(env["SPARK_SCP_BIN"])
    wslpath = scp.parent / "wslpath"
    wslpath.write_text(
        "#!/bin/sh\nprintf 'WINDOWS_PATH:%s\\n' \"$2\"\n",
        encoding="utf-8",
    )
    wslpath.chmod(0o755)
    env["SPARK_SCP_PATH_STYLE"] = "windows"
    env["PATH"] = f"{scp.parent}:{env['PATH']}"

    completed = _run(root, "--apply", "example", env=env)

    assert completed.returncode == 0, completed.stderr
    assert all(
        call["argv"][-2].startswith("WINDOWS_PATH:")
        for call in _json_lines(scp_log)
    )


def test_posix_style_scp_wrapper_named_exe_keeps_a_posix_source_path(
    tmp_path: Path,
) -> None:
    root, _ = _release_root(tmp_path, nodes=("spark1",))
    env, _, scp_log = _fake_remote_environment(tmp_path)
    scp = Path(env["SPARK_SCP_BIN"])
    misleading_name = scp.with_suffix(".exe")
    scp.rename(misleading_name)
    env["SPARK_SCP_BIN"] = str(misleading_name)
    env["SPARK_SCP_PATH_STYLE"] = "posix"

    completed = _run(root, "--apply", "example", env=env)

    assert completed.returncode == 0, completed.stderr
    assert all(
        call["argv"][-2].startswith(str(root))
        for call in _json_lines(scp_log)
    )


def test_invalid_scp_path_style_fails_before_remote_commands(tmp_path: Path) -> None:
    root, _ = _release_root(tmp_path, nodes=("spark1",))
    env, ssh_log, scp_log = _fake_remote_environment(tmp_path)
    env["SPARK_SCP_PATH_STYLE"] = "windows; unsafe"

    completed = _run(root, "--apply", "example", env=env)

    assert completed.returncode == 2
    assert "SPARK_SCP_PATH_STYLE must be posix or windows" in completed.stderr
    assert _json_lines(ssh_log) == []
    assert _json_lines(scp_log) == []


def test_apply_is_idempotent_when_both_installed_releases_are_identical(
    tmp_path: Path,
) -> None:
    root, _ = _release_root(tmp_path)
    env, ssh_log, scp_log = _fake_remote_environment(tmp_path, probe="identical")

    completed = _run(root, "--apply", "example", env=env)

    assert completed.returncode == 0, completed.stderr
    assert len(_json_lines(ssh_log)) == 2
    assert _json_lines(scp_log) == []
    assert completed.stdout.count("already installed") == 2


def test_apply_refuses_to_replace_different_installed_content(tmp_path: Path) -> None:
    root, _ = _release_root(tmp_path)
    env, ssh_log, scp_log = _fake_remote_environment(tmp_path, probe="different")

    completed = _run(root, "--apply", "example", env=env)

    assert completed.returncode != 0
    assert "refusing to replace" in completed.stderr
    assert len(_json_lines(ssh_log)) == 1
    assert _json_lines(scp_log) == []


def test_filesystem_remote_executes_atomic_install_and_idempotent_probe(
    tmp_path: Path,
) -> None:
    root, digest = _release_root(tmp_path)
    environment, remote_root = _filesystem_remote_environment(tmp_path)

    first = _run(root, "--apply", "example", env=environment)
    second = _run(root, "--apply", "example", env=environment)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert second.stdout.count("already installed") == 2
    for alias in ("dgx-spark-1", "dgx-spark-2"):
        installed = (
            remote_root
            / alias
            / "opt/spark/model-adapters/example/releases"
            / digest
        )
        assert (installed / "bin/adapter").read_text(encoding="utf-8") == (
            "#!/bin/sh\nexit 0\n"
        )
        assert (installed / "bin/adapter").stat().st_mode & 0o777 == 0o755
        assert (installed / "config/common.env").stat().st_mode & 0o777 == 0o644
        assert not any(path.name.startswith(f".{digest}.tmp-") for path in installed.parent.iterdir())


def test_filesystem_remote_repairs_modes_not_preserved_by_scp(tmp_path: Path) -> None:
    root, digest = _release_root(tmp_path)
    environment, remote_root = _filesystem_remote_environment(
        tmp_path, preserve_modes=False
    )

    completed = _run(root, "--apply", "example", env=environment)

    assert completed.returncode == 0, completed.stderr
    for alias in ("dgx-spark-1", "dgx-spark-2"):
        installed = (
            remote_root
            / alias
            / "opt/spark/model-adapters/example/releases"
            / digest
        )
        assert (installed / "bin/adapter").stat().st_mode & 0o777 == 0o755
        assert (installed / "config/common.env").stat().st_mode & 0o777 == 0o644


def test_filesystem_remote_rejects_changed_extra_and_symlinked_content(
    tmp_path: Path,
) -> None:
    root, digest = _release_root(tmp_path)
    environment, remote_root = _filesystem_remote_environment(tmp_path)
    first = _run(root, "--apply", "example", env=environment)
    assert first.returncode == 0, first.stderr
    installed = (
        remote_root
        / "dgx-spark-1/opt/spark/model-adapters/example/releases"
        / digest
    )

    (installed / "bin/adapter").write_text("changed\n", encoding="utf-8")
    (installed / "extra").write_text("unexpected\n", encoding="utf-8")
    (installed / "config/link").symlink_to(installed / "config/common.env")
    completed = _run(root, "--apply", "example", env=environment)

    assert completed.returncode != 0
    assert "refusing to replace different release" in completed.stderr


def test_filesystem_remote_refuses_symlinked_install_ancestors(tmp_path: Path) -> None:
    root, _ = _release_root(tmp_path)
    environment, remote_root = _filesystem_remote_environment(tmp_path)
    trusted = remote_root / "dgx-spark-1/opt/spark/model-adapters"
    outside = tmp_path / "outside"
    outside.mkdir()
    (trusted / "example").symlink_to(outside, target_is_directory=True)

    completed = _run(root, "--apply", "example", env=environment)

    assert completed.returncode != 0
    assert "release probe failed" in completed.stderr
    assert not any(outside.iterdir())


def test_filesystem_remote_destination_race_never_replaces_content(
    tmp_path: Path,
) -> None:
    root, digest = _release_root(tmp_path)
    environment, remote_root = _filesystem_remote_environment(
        tmp_path, race_destination=True
    )

    completed = _run(root, "--apply", "example", env=environment)

    assert completed.returncode != 0
    final = (
        remote_root
        / "dgx-spark-1/opt/spark/model-adapters/example/releases"
        / digest
    )
    assert final.is_dir()
    assert list(final.iterdir()) == []
