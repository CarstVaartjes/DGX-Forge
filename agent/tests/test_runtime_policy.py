from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from pathlib import Path

import pytest
from dgx_agent.config import AgentConfig
from dgx_agent.main import build_agent
from dgx_agent.releases import ReleaseInstaller
from dgx_agent.runtime_policy import RuntimePolicy, RuntimePolicyError
from dgx_agent.workloads import WorkloadOperations


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write(path: Path, raw: bytes, mode: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(mode)
    return path


def _elf(tmp_path: Path) -> Path:
    source = tmp_path / "oras.c"
    source.write_text("int main(void) { return 0; }\n")
    target = tmp_path / "oras"
    subprocess.run(["cc", "-o", str(target), str(source)], check=True)
    target.chmod(0o555)
    return target


def policy_fixture(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    architecture = "aarch64" if platform.machine() in {"aarch64", "arm64"} else "x86_64"
    unhashed_executable = _elf(tmp_path)
    executable_digest = hashlib.sha256(unhashed_executable.read_bytes()).hexdigest()
    executable = tmp_path / f"opt/dgx-forge/third-party/oras/{executable_digest}/oras"
    executable.parent.mkdir(parents=True)
    unhashed_executable.rename(executable)
    auth = _write(
        tmp_path / "var/lib/dgx-forge-agent/registry-auth.json", b"{}\n", 0o600
    )
    bootstrap = _write(tmp_path / "etc/dgx-forge-agent/tuf-root.json", b"{}\n", 0o644)
    metadata = tmp_path / "var/lib/dgx-forge-agent/tuf/metadata"
    targets = tmp_path / "var/lib/dgx-forge-agent/tuf/targets"
    releases = tmp_path / "var/lib/dgx-forge/releases"
    staging = tmp_path / "var/lib/dgx-forge/release-staging"
    for directory in (metadata, targets, releases, staging):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    document = {
        "adapter": {
            "adapter_id": "spark-runtime-v1",
            "executable_relative_path": "bin/runtime-adapter",
            "output_limit_bytes": 65536,
            "timeout_seconds": 60,
        },
        "architecture": architecture,
        "oras": {
            "auth_path": str(auth),
            "executable": str(executable),
            "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "version": "1.3.3",
        },
        "registry_origin": "https://registry.example:8443",
        "release_root": str(releases),
        "repository": "dgx-forge/releases",
        "schema_version": 1,
        "staging_root": str(staging),
        "tuf": {
            "bootstrap_root_path": str(bootstrap),
            "bootstrap_root_sha256": hashlib.sha256(bootstrap.read_bytes()).hexdigest(),
            "metadata_root": str(metadata),
            "target_root": str(targets),
        },
    }
    policy = _write(
        tmp_path / "etc/dgx-forge-agent/runtime-policy.json",
        _canonical(document),
        0o644,
    )
    return policy, document


def test_runtime_policy_loads_only_exact_installed_transport_and_roots(
    tmp_path: Path,
) -> None:
    path, document = policy_fixture(tmp_path)

    policy = RuntimePolicy._load_for_test(path, tmp_path)
    policy.verify_installed()

    assert policy.architecture == document["architecture"]
    assert policy.registry_origin == "https://registry.example:8443"
    assert policy.repository == "dgx-forge/releases"
    assert policy.oras.version == "1.3.3"
    assert policy.adapter.adapter_id == "spark-runtime-v1"
    assert policy.adapter.executable_relative_path == "bin/runtime-adapter"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=True),
        lambda d: d.update(architecture="mips64"),
        lambda d: d.update(registry_origin="http://registry.example"),
        lambda d: d.update(repository="../escape"),
        lambda d: d["oras"].update(version="latest"),
        lambda d: d["oras"].update(sha256="A" * 64),
        lambda d: d["adapter"].update(timeout_seconds=61),
        lambda d: d["tuf"].update(metadata_root="relative"),
    ],
)
def test_runtime_policy_rejects_unknown_unreviewed_or_unsafe_values(
    tmp_path: Path, mutate
) -> None:
    path, document = policy_fixture(tmp_path)
    mutate(document)
    path.write_bytes(_canonical(document))

    with pytest.raises(RuntimePolicyError):
        RuntimePolicy._load_for_test(path, tmp_path)


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("oras", "executable"),
        ("oras", "auth_path"),
        ("tuf", "bootstrap_root_path"),
        ("tuf", "metadata_root"),
        ("tuf", "target_root"),
        (None, "release_root"),
        (None, "staging_root"),
    ],
)
def test_runtime_policy_rejects_alternate_absolute_installed_paths(
    tmp_path: Path, section: str | None, field: str
) -> None:
    path, document = policy_fixture(tmp_path)
    target = document if section is None else document[section]
    assert isinstance(target, dict)
    target[field] = str(tmp_path / "safe-root-owned-alternative" / field)
    path.write_bytes(_canonical(document))

    with pytest.raises(RuntimePolicyError, match="installed location"):
        RuntimePolicy._load_for_test(path, tmp_path)


def test_runtime_policy_rejects_duplicate_symlink_hardlink_and_tampered_artifact(
    tmp_path: Path,
) -> None:
    path, document = policy_fixture(tmp_path)
    raw = _canonical(document)
    path.write_bytes(raw[:-2] + b',"schema_version":1}\n')
    with pytest.raises(RuntimePolicyError, match="duplicate"):
        RuntimePolicy._load_for_test(path, tmp_path)

    path.write_bytes(raw)
    linked_policy = tmp_path / "linked-policy.json"
    linked_policy.symlink_to(path)
    with pytest.raises(RuntimePolicyError):
        RuntimePolicy._load_for_test(linked_policy, tmp_path)

    executable = Path(document["oras"]["executable"])
    hardlink = executable.with_name("oras-link")
    os.link(executable, hardlink)
    with pytest.raises(RuntimePolicyError):
        RuntimePolicy._load_for_test(path, tmp_path).verify_installed()
    hardlink.unlink()
    executable.chmod(0o755)
    executable.write_bytes(b"tampered")
    executable.chmod(0o555)
    with pytest.raises(RuntimePolicyError):
        RuntimePolicy._load_for_test(path, tmp_path).verify_installed()


def test_build_agent_constructs_release_and_workload_handlers_with_one_credential_store(
    tmp_path: Path, monkeypatch
) -> None:
    policy_path, _ = policy_fixture(tmp_path)
    runtime = RuntimePolicy._load_for_test(policy_path, tmp_path)
    state = tmp_path / "agent-state"
    ca = _write(tmp_path / "ca.pem", b"ca", 0o644)
    certificate = _write(tmp_path / "cert.pem", b"cert", 0o644)
    key = _write(tmp_path / "key.pem", b"key", 0o600)
    nvidia = _write(tmp_path / "nvidia-policy.json", b"{}\n", 0o644)
    token = _write(tmp_path / "token", b"A" * 43 + b"\n", 0o600)
    config = AgentConfig(
        control_origin="https://control.example:8443",
        enrollment_origin="https://enroll.example:8443",
        node_id="spk_0123456789abcdef0123456789abcdef",
        certificate_path=certificate,
        private_key_path=key,
        ca_path=ca,
        poll_min_seconds=1,
        poll_max_seconds=60,
        state_root=state,
        installed_policy_path=nvidia,
        runtime_policy_path=policy_path,
        enrollment_token_path=token,
    )
    sentinel_nvidia = object()
    monkeypatch.setattr(
        "dgx_agent.main.InstalledPolicy.load", lambda _: sentinel_nvidia
    )
    monkeypatch.setattr("dgx_agent.main.RuntimePolicy.load", lambda _: runtime)

    agent = build_agent(config)

    assert isinstance(agent._context.releases, ReleaseInstaller)
    assert isinstance(agent._context.workloads, WorkloadOperations)
    assert agent._context.probe.policy is sentinel_nvidia
    transport = agent._context.releases._transport
    trust = agent._context.releases._trust
    assert transport._policy.credential_provider is agent._credentials
    assert trust._fetcher._credential_provider is agent._credentials
    assert transport._policy.registry_origin == "https://registry.example:8443"
    assert transport._policy.repository == "dgx-forge/releases"
