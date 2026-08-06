from __future__ import annotations

import hashlib
import io
import json
import struct
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/build-spark-platform-payloads"


def _arm64_elf() -> bytes:
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HH", header, 16, 3, 183)
    return bytes(header) + b"agent-payload"


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    agent = tmp_path / "agent"
    supervisor = tmp_path / "supervisor"
    tooling_bin = tmp_path / "bin"
    tooling_policy = tmp_path / "policy"
    agent.write_bytes(_arm64_elf())
    agent.chmod(0o755)
    supervisor.write_text("#!/usr/bin/python3 -I\nprint('supervisor')\n")
    supervisor.chmod(0o755)
    tooling_bin.mkdir()
    (tooling_bin / "inspect-node").write_text("#!/bin/sh\nexit 0\n")
    (tooling_bin / "inspect-node").chmod(0o755)
    tooling_policy.mkdir()
    (tooling_policy / "default.json").write_text('{"schema_version":1}\n')
    return agent, supervisor, tooling_bin, tooling_policy


def _run(
    agent: Path,
    supervisor: Path,
    tooling_bin: Path,
    tooling_policy: Path,
    output: Path,
    *,
    architecture: str = "linux-arm64",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            SCRIPT,
            "--agent",
            agent,
            "--supervisor",
            supervisor,
            "--tooling-bin",
            tooling_bin,
            "--tooling-policy",
            tooling_policy,
            "--architecture",
            architecture,
            "--output",
            output,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_builder_emits_exact_deterministic_arm64_payload_set(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"

    result = _run(*inputs, first)
    repeated = _run(*inputs, second)

    assert result.returncode == repeated.returncode == 0, result.stderr
    assert sorted(path.name for path in first.iterdir()) == [
        "dgx-agent",
        "dgx-agent-supervisor",
        "dgx-forge-tooling",
        "spark-platform-payloads.json",
    ]
    assert (first / "dgx-agent").read_bytes() == _arm64_elf()
    assert (first / "dgx-agent-supervisor").read_text().startswith(
        "#!/usr/bin/python3 -I\n"
    )
    assert (first / "dgx-forge-tooling").read_bytes() == (
        second / "dgx-forge-tooling"
    ).read_bytes()
    assert (first / "spark-platform-payloads.json").read_bytes() == (
        second / "spark-platform-payloads.json"
    ).read_bytes()

    receipt_raw = (first / "spark-platform-payloads.json").read_bytes()
    receipt = json.loads(receipt_raw)
    assert receipt_raw == (
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    assert receipt["schema_version"] == 1
    assert receipt["architecture"] == "linux-arm64"
    assert set(receipt["payloads"]) == {"agent", "supervisor", "tooling"}
    for key, name in (
        ("agent", "dgx-agent"),
        ("supervisor", "dgx-agent-supervisor"),
        ("tooling", "dgx-forge-tooling"),
    ):
        payload = first / name
        assert receipt["payloads"][key] == {
            "name": name,
            "sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
            "size": payload.stat().st_size,
        }

    with tarfile.open(fileobj=io.BytesIO((first / "dgx-forge-tooling").read_bytes())) as archive:
        assert archive.getnames() == [
            "bin/inspect-node",
            "policy/default.json",
        ]
        for member in archive.getmembers():
            assert (member.uid, member.gid, member.mtime, member.uname, member.gname) == (
                0,
                0,
                0,
                "root",
                "root",
            )


def test_builder_rejects_wrong_architecture_unsafe_input_and_existing_output(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    wrong = _run(*inputs, tmp_path / "wrong", architecture="linux-x86_64")
    assert wrong.returncode == 2
    assert not (tmp_path / "wrong").exists()

    unsafe_agent = tmp_path / "unsafe-agent"
    unsafe_agent.symlink_to(inputs[0])
    unsafe = _run(
        unsafe_agent,
        inputs[1],
        inputs[2],
        inputs[3],
        tmp_path / "unsafe",
    )
    assert unsafe.returncode == 2
    assert not (tmp_path / "unsafe").exists()

    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "keep"
    marker.write_text("operator data\n")
    collision = _run(*inputs, existing)
    assert collision.returncode == 2
    assert marker.read_text() == "operator data\n"


def test_builder_refuses_world_writable_tooling_member(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    member = inputs[2] / "inspect-node"
    member.chmod(0o777)

    result = _run(*inputs, tmp_path / "output")

    assert result.returncode == 2
    assert not (tmp_path / "output").exists()
