from __future__ import annotations

import os
import platform
import shutil
import struct
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / "agent/tools/build-slot-artifact"
PROTOCOL = ROOT / "inventory/wheels/dgx_agent_protocol-1.0.0-py3-none-any.whl"


def _architecture() -> str:
    return "aarch64" if platform.machine() in {"aarch64", "arm64"} else "x86_64"


def test_builder_rejects_missing_inputs_cross_architecture_and_output_symlink(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.whl"
    output = tmp_path / "dgx-forge-agent"
    cross = "aarch64" if _architecture() == "x86_64" else "x86_64"

    for argv in (
        (missing, PROTOCOL, output, _architecture()),
        (PROTOCOL, PROTOCOL, output, cross),
    ):
        result = subprocess.run(
            [
                str(BUILDER),
                "--agent-wheel",
                str(argv[0]),
                "--protocol-wheel",
                str(argv[1]),
                "--output",
                str(argv[2]),
                "--architecture",
                argv[3],
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode != 0

    output.symlink_to(tmp_path / "elsewhere")
    result = subprocess.run(
        [
            str(BUILDER),
            "--agent-wheel",
            str(PROTOCOL),
            "--protocol-wheel",
            str(PROTOCOL),
            "--output",
            str(output),
            "--architecture",
            _architecture(),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0


def test_builds_one_self_contained_native_elf_with_isolated_module_smoke(
    tmp_path: Path,
) -> None:
    distribution = tmp_path / "distribution"
    subprocess.run(
        [
            "uv",
            "build",
            "--project",
            str(ROOT / "agent"),
            "--wheel",
            "--out-dir",
            str(distribution),
        ],
        check=True,
    )
    wheel = next(distribution.glob("dgx_agent-*.whl"))
    artifact = tmp_path / "outside/dgx-forge-agent"
    artifact.parent.mkdir()
    subprocess.run(
        [
            str(BUILDER),
            "--agent-wheel",
            str(wheel),
            "--protocol-wheel",
            str(PROTOCOL),
            "--output",
            str(artifact),
            "--architecture",
            _architecture(),
        ],
        check=True,
    )

    raw = artifact.read_bytes()[:64]
    assert raw[:7] == b"\x7fELF\x02\x01\x01"
    assert (
        struct.unpack_from("<H", raw, 18)[0]
        == {"x86_64": 62, "aarch64": 183}[_architecture()]
    )
    assert [item.name for item in artifact.parent.iterdir()] == ["dgx-forge-agent"]

    isolated_home = tmp_path / "empty-home"
    isolated_home.mkdir()
    environment = {
        "HOME": str(isolated_home),
        "LANG": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONHOME": "/nonexistent",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "/nonexistent",
    }
    for arguments, expected in (
        (["--help"], None),
        (["--packaged-module-smoke"], "packaged-agent-modules-ok\n"),
    ):
        result = subprocess.run(
            [str(artifact), *arguments],
            cwd=isolated_home,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        if expected is not None:
            assert result.stdout == expected
    assert shutil.which("dgx-forge-agent", path=str(artifact.parent)) == str(artifact)


def test_builder_snapshots_wheels_and_ignores_hostile_path_network_and_empty_cache(
    tmp_path: Path,
) -> None:
    distribution = tmp_path / "distribution"
    subprocess.run(
        [
            "uv",
            "build",
            "--project",
            str(ROOT / "agent"),
            "--wheel",
            "--out-dir",
            str(distribution),
        ],
        check=True,
    )
    wheel = next(distribution.glob("dgx_agent-*.whl"))
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    invoked = tmp_path / "hostile-uv-invoked"
    fake_uv = hostile / "uv"
    fake_uv.write_text(f"#!/bin/sh\ntouch {invoked}\nexit 91\n")
    fake_uv.chmod(0o755)
    output = tmp_path / "dgx-forge-agent"
    environment = {
        **os.environ,
        "PATH": f"{hostile}:/usr/bin:/bin",
        "UV_CACHE_DIR": str(tmp_path / "empty-cache"),
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "DGX_SLOT_SUBSTITUTE_INPUT_TEST": "1",
    }

    built = subprocess.run(
        [
            str(BUILDER),
            "--agent-wheel",
            str(wheel),
            "--protocol-wheel",
            str(PROTOCOL),
            "--output",
            str(output),
            "--architecture",
            _architecture(),
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )

    assert built.returncode == 0, built.stderr
    assert not invoked.exists()
    assert output.read_bytes().startswith(b"\x7fELF")
