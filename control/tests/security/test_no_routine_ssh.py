from __future__ import annotations

import ast
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "control/src/dgx_control"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def test_production_api_and_worker_do_not_import_direct_runtime_or_subprocess() -> None:
    for name in ("api.py", "worker.py"):
        path = PACKAGE / name
        imports = _imports(path)
        source = path.read_text()
        assert "subprocess" not in imports
        assert "dgx_control.runtime" not in imports
        assert "dgx_control.legacy_runtime" not in imports
        assert "RuntimeHandlers" not in source
        assert "LegacyRuntimeHandlers" not in source

    worker = (PACKAGE / "worker.py").read_text()
    worker_imports = _imports(PACKAGE / "worker.py")
    assert {
        "git_policy",
        "code_host",
        "repository",
        "hermes_routes",
        "legacy_runtime",
        "runtime",
        "subprocess",
    }.isdisjoint(worker_imports)
    for dynamic_escape in (
        "importlib",
        "__import__",
        "os.system",
        "os.popen",
        "eval(",
        "exec(",
    ):
        assert dynamic_escape not in worker
    for forbidden in (
        "dgx_control.git_policy",
        "dgx_control.code_host",
        "dgx_control.repository",
        "dgx_control.hermes_routes",
        "RepositoryService",
        "GitPolicy",
    ):
        assert forbidden not in worker


def test_runtime_module_contains_no_process_or_transport_implementation() -> None:
    path = PACKAGE / "runtime.py"
    imports = _imports(path)
    source = path.read_text()

    assert "subprocess" not in imports
    assert "RuntimeHandlers" not in source
    assert "run_bounded" not in source
    assert "ssh" not in source.lower()


def test_legacy_runtime_is_explicitly_isolated_from_production_modules() -> None:
    legacy = PACKAGE / "legacy_runtime.py"
    assert legacy.is_file()
    source = legacy.read_text()
    assert "explicit-test-only" in source
    assert "LegacyRuntimeHandlers" in source
    assert "subprocess" in _imports(legacy)


def test_production_worker_image_does_not_install_direct_transport_tools() -> None:
    dockerfile = (ROOT / "control/Dockerfile").read_text()
    worker = dockerfile.split(" AS worker", 1)[1].split(" AS api", 1)[0]
    api = dockerfile.split(" AS api", 1)[1]

    assert "apt-get" not in worker
    assert "openssh-client" not in worker
    assert " git" not in worker
    assert "git openssh-client" in api


def test_production_worker_has_no_cluster_egress_network() -> None:
    compose = (ROOT / "deploy/compose/compose.yaml").read_text()
    worker = compose.split("\n  control-worker:\n", 1)[1].split(
        "\n  workload-signer-runtime-init:\n", 1
    )[0]

    assert "cluster-egress" not in worker
    for forbidden in (
        "/repository",
        "git-signing-key",
        "token-signing-key",
        "metrics-token",
        "DGX_REPOSITORY_PATH",
        "DGX_GIT_SIGNING_KEY_FILE",
    ):
        assert forbidden not in worker
    assert "CONTROL_WORKER_IMAGE" in worker


def test_built_worker_image_contains_no_direct_transport_executable() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is unavailable")
    if subprocess.run(
        ["docker", "info"],
        capture_output=True,
        check=False,
    ).returncode != 0:
        pytest.skip("Docker daemon is unavailable")
    image = "dgx-control-worker:test-no-routine-ssh"
    build = subprocess.run(
        [
            "docker",
            "build",
            "--file",
            "control/Dockerfile",
            "--target",
            "worker",
            "--tag",
            image,
            ".",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, build.stderr
    inspect = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "/bin/sh",
            image,
            "-eu",
            "-c",
            (
                "for executable in ssh scp git sparkctl; do "
                "! command -v \"$executable\"; done; "
                "test ! -e /repository; test ! -e /spark-profiles; "
                "test ! -e /scripts"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert inspect.returncode == 0, inspect.stderr
