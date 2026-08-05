from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import pytest
from dgx_agent.update import (
    AgentArtifact,
    AgentReleaseIdentity,
    AgentUpdateError,
    AgentUpdater,
    SupervisorSlotState,
)


def _elf(machine: int = 183, size: int = 4096) -> bytes:
    header = bytearray(max(size, 64))
    header[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HH", header, 16, 2, machine)
    return bytes(header)


class FakeTrust:
    def __init__(self) -> None:
        self.authorized = True
        self.calls: list[tuple[AgentArtifact, AgentReleaseIdentity]] = []

    def authorize(
        self, artifact: AgentArtifact, release: AgentReleaseIdentity
    ) -> None:
        self.calls.append((artifact, release))
        if not self.authorized:
            raise AgentUpdateError("platform release is not authorized")


class FakeTransport:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.fail = False
        self.destinations: list[Path] = []

    def fetch(self, artifact: AgentArtifact, destination: Path) -> None:
        self.destinations.append(destination)
        destination.write_bytes(self.content[: len(self.content) // 2])
        if self.fail:
            raise AgentUpdateError("download interrupted")
        with destination.open("ab") as stream:
            stream.write(self.content[len(self.content) // 2 :])


class FakeSupervisor:
    def __init__(self) -> None:
        self.state = SupervisorSlotState(
            active_slot="A",
            previous_slot=None,
            status="stable",
            slot_sha256={"A": "1" * 64, "B": None},
        )
        self.requests: list[Path] = []

    def inspect(self) -> SupervisorSlotState:
        return self.state

    def notify(self, request_path: Path) -> None:
        self.requests.append(request_path)


def _inputs(content: bytes) -> tuple[AgentArtifact, AgentReleaseIdentity]:
    digest = hashlib.sha256(content).hexdigest()
    return (
        AgentArtifact(
            architecture="linux-arm64",
            reference=f"ghcr.io/example/dgx-agent@sha256:{digest}",
            sha256=digest,
            size=len(content),
        ),
        AgentReleaseIdentity(
            platform_version="1.2.0",
            build_digest=f"sha256:{digest}",
            protocol_minimum=1,
            protocol_maximum=2,
        ),
    )


def _updater(tmp_path: Path, content: bytes):
    trust = FakeTrust()
    transport = FakeTransport(content)
    supervisor = FakeSupervisor()
    updater = AgentUpdater(
        architecture="linux-arm64",
        protocol_version=1,
        staging_root=tmp_path / "staging",
        runtime_root=tmp_path / "run",
        trust=trust,
        transport=transport,
        supervisor=supervisor,
        available_bytes=lambda: 1024 * 1024,
    )
    return updater, trust, transport, supervisor


def test_agent_update_plans_and_stages_only_the_inactive_slot(tmp_path: Path) -> None:
    content = _elf()
    updater, trust, _transport, supervisor = _updater(tmp_path, content)
    artifact, release = _inputs(content)

    plan = updater.plan(artifact, release)
    pending = updater.apply(plan)

    assert plan.previous_slot == "A"
    assert plan.target_slot == "B"
    assert pending.target_slot == "B"
    assert pending.previous_slot == "A"
    assert pending.platform_version == "1.2.0"
    assert trust.calls == [(artifact, release), (artifact, release)]
    installed = tmp_path / "staging" / f"{artifact.sha256}.agent"
    assert installed.read_bytes() == content
    assert installed.stat().st_mode & 0o777 == 0o500
    request = tmp_path / "run/activation-request.json"
    assert request.is_file()
    assert supervisor.requests == [request]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("architecture", "architecture"),
        ("protocol", "protocol"),
        ("digest", "reference digest"),
        ("space", "disk space"),
        ("pending", "stable"),
    ],
)
def test_agent_update_rejects_incompatible_or_unsafe_plan(
    tmp_path: Path, change: str, message: str
) -> None:
    content = _elf()
    updater, _trust, _transport, supervisor = _updater(tmp_path, content)
    artifact, release = _inputs(content)
    if change == "architecture":
        artifact = AgentArtifact(**{**artifact.__dict__, "architecture": "linux-x86_64"})
    elif change == "protocol":
        release = AgentReleaseIdentity(**{**release.__dict__, "protocol_minimum": 2})
    elif change == "digest":
        with pytest.raises(AgentUpdateError, match=message):
            AgentArtifact(**{**artifact.__dict__, "sha256": "2" * 64})
        return
    elif change == "space":
        updater._available_bytes = lambda: 1
    else:
        supervisor.state = SupervisorSlotState(
            active_slot="A",
            previous_slot="B",
            status="pending",
            slot_sha256={"A": "1" * 64, "B": "2" * 64},
        )

    with pytest.raises(AgentUpdateError, match=message):
        updater.plan(artifact, release)


def test_interrupted_agent_download_never_publishes_activation(tmp_path: Path) -> None:
    content = _elf()
    updater, _trust, transport, supervisor = _updater(tmp_path, content)
    artifact, release = _inputs(content)
    plan = updater.plan(artifact, release)
    transport.fail = True

    with pytest.raises(AgentUpdateError, match="interrupted"):
        updater.apply(plan)

    assert supervisor.requests == []
    assert not (tmp_path / "run/activation-request.json").exists()
    assert not (tmp_path / "staging" / f"{artifact.sha256}.agent").exists()
