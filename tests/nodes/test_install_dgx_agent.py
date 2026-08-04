from __future__ import annotations

import grp
import hashlib
import json
import os
import platform
import pwd
import runpy
import shutil
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "nodes/bin/install-dgx-agent"
NVIDIA_LOCK = ROOT / "nodes/vendor/nvidia-manageability.lock.json"


def test_nvidia_lock_binds_exact_archive_license_provenance_and_installed_subset() -> (
    None
):
    document = json.loads(NVIDIA_LOCK.read_text())

    assert document["schema_version"] == 1
    assert document["filename"] == (
        "enterprise-lifecycle-integration-scripts-20260520-1602.zip"
    )
    assert document["version"] == "0.1.0"
    assert document["sha256"] == (
        "0eb1c93dd839b6bd4136cc8b79ea04a1e44fd637ff6afa6ee9568951a4c179f3"
    )
    assert len(document["archive_members"]) == 132
    expected_installed = {
        "LICENSE": (
            "license",
            "f07648473079bb1d864c5d8bb011e0d06efd69a31f3be1d4654ef5da3ff7bcab",
            0o444,
            "MIT",
        ),
        "bin/common/__init__.py": (
            "support",
            "a3b4329f7500a2f9d95369ba32b3eb563c27a76d6d96d9f98dac1c1fc41b938a",
            0o444,
            None,
        ),
        "bin/common/asset_id.py": (
            "support",
            "35277c9d42c97960434f10e7f8dfda0a7e12cfbe00aec0d86ea88099c5ac9eca",
            0o444,
            None,
        ),
        "bin/common/cli_base.py": (
            "support",
            "0b1f72a2056cbb5a3c717e7853b7f4d986a4b91b7920eadab68888b101f1b1da",
            0o444,
            None,
        ),
        "bin/common/output.py": (
            "support",
            "6938255c277aa5b3b2e805a2cbfdc52d86c5d19910591cb42272a7eb280e2426",
            0o444,
            None,
        ),
        "bin/device_identity.py": (
            "device_identity",
            "110acb65e54092a63d93f8d0448855717323c7251bbaf661a7d6cb41836f2dcf",
            0o555,
            "1.1.0",
        ),
        "bin/driver_inventory_reporter.py": (
            "driver_inventory_reporter",
            "f5f90c05f077f1cd6fa387d1f6eac3b7f40b7d859c6e5886c73ec03629fdfc26",
            0o555,
            "1.0.0",
        ),
        "bin/firmware_reporter.py": (
            "firmware_reporter",
            "c5887cb8b456295ea937a44cf05d8c1a3fa64b2ac8239f35be61e8deb358d387",
            0o555,
            "1.0.0",
        ),
        "bin/hardware_config.py": (
            "hardware_config",
            "07c05c03f65e9b707bc18ebd2ec010ac1622701fa0b87858014a5b71fd1af5bb",
            0o555,
            "1.0.0",
        ),
        "bin/os_build_identity.py": (
            "os_build_identity",
            "ee2f06d7ae25438ed0a7258eeeecdde76dba24c5c82f9dec510c361b9d75f6f9",
            0o555,
            "1.0.0",
        ),
        "bin/reset_reason_reporter.py": (
            "reset_reason_reporter",
            "212b49f894e4703cc85743217a0a9d9f2bb5891702266df84b907df960d83774",
            0o555,
            "1.1.0",
        ),
        "bin/spark_diagctl.py": (
            "spark_diagctl_health",
            "03de23664d3a24295ce605075be957328f47c24fa37afb7bbfe60988cbee42c2",
            0o555,
            "1.1.0",
        ),
    }
    assert {
        path: (
            policy["role"],
            policy["sha256"],
            policy["mode"],
            policy.get("version"),
        )
        for path, policy in document["installed_files"].items()
    } == expected_installed
    assert document["license"] == {
        "path": "LICENSE",
        "sha256": "f07648473079bb1d864c5d8bb011e0d06efd69a31f3be1d4654ef5da3ff7bcab",
        "spdx": "MIT",
    }
    assert document["provenance_sha256"] == (
        "6991ef9274eeef825ad989227e150e5b02c08a5780ede91729ae48bd39d3e57c"
    )
    assert document["source"] == (
        "https://docscontent.nvidia.com/dc/04/5167e1c14532bac843d48d29bf36/"
        "enterprise-lifecycle-integration-scripts-20260520-1602.zip"
    )
    assert document["provenance"] == {
        "artifact": "enterprise-lifecycle-integration-scripts-20260520-1602.zip",
        "sha256": document["sha256"],
        "source": document["source"],
        "version": "0.1.0",
    }
    assert document["installed_files"]["bin/device_identity.py"] == {
        "mode": 0o555,
        "role": "device_identity",
        "sha256": "110acb65e54092a63d93f8d0448855717323c7251bbaf661a7d6cb41836f2dcf",
        "size": 14452,
        "version": "1.1.0",
    }


def test_installer_exists_as_networkless_node_local_primitive(tmp_path: Path) -> None:
    result = subprocess.run(
        [str(INSTALLER), "--help"],
        env={**os.environ, "DGX_INSTALL_TEST_ROOT": str(tmp_path / "host")},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--node-id" in result.stdout
    assert "--nvidia-bundle" in result.stdout
    assert "--agent-artifact" in result.stdout


def test_installer_rejects_noncanonical_node_before_mutation(tmp_path: Path) -> None:
    result = subprocess.run(
        [str(INSTALLER), "--node-id", "spark1"],
        env={**os.environ, "DGX_INSTALL_TEST_ROOT": str(tmp_path / "host")},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 64
    assert not (tmp_path / "host").exists()


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: bytes, mode: int) -> Path:
    path.write_bytes(value)
    path.chmod(mode)
    return path


def _elf(tmp_path: Path, name: str) -> Path:
    source = tmp_path / f"{name}.c"
    source.write_text("int main(void) { return 0; }\n")
    target = tmp_path / name
    subprocess.run(
        ["cc", "-O2", "-o", str(target), str(source)],
        check=True,
        capture_output=True,
    )
    target.chmod(0o755)
    return target


@pytest.fixture
def installer_inputs(tmp_path: Path) -> dict[str, object]:
    machine = platform.machine()
    architecture = "aarch64" if machine in {"aarch64", "arm64"} else "x86_64"
    agent = _elf(tmp_path, "agent")
    oras = _elf(tmp_path, "oras")
    health = _write(tmp_path / "collect-health", b"#!/bin/sh\nexit 0\n", 0o755)
    ca = tmp_path / "ca.pem"
    ca_key = tmp_path / "fixture-ca-key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=DGX Forge fixture CA",
            "-days",
            "1",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca),
        ],
        check=True,
        capture_output=True,
    )
    ca.chmod(0o644)
    tuf = _write(
        tmp_path / "root.json", _canonical({"signed": {}, "signatures": []}), 0o644
    )
    auth = _write(tmp_path / "auth.json", _canonical({"auths": {}}), 0o600)
    token = _write(tmp_path / "enrollment-token", b"A" * 43 + b"\n", 0o600)
    site_document = {
        "architecture": architecture,
        "control_origin": "https://control.example:8443",
        "enrollment_origin": "https://enroll.example:8443",
        "fabric_pairs": [{"hca": "rocep1s0f1", "interface": "enp1s0f1np1"}],
        "poll_max_seconds": 60,
        "poll_min_seconds": 1,
        "registry_origin": "https://registry.example:8443",
        "repository": "dgx-forge/releases",
        "schema_version": 1,
    }
    site = _write(tmp_path / "site.json", _canonical(site_document), 0o644)

    installed_contract = {
        "bin/device_identity.py": ("device_identity", "1.1.0", 0o555),
        "bin/hardware_config.py": ("hardware_config", "1.0.0", 0o555),
        "bin/firmware_reporter.py": ("firmware_reporter", "1.0.0", 0o555),
        "bin/os_build_identity.py": ("os_build_identity", "1.0.0", 0o555),
        "bin/driver_inventory_reporter.py": (
            "driver_inventory_reporter",
            "1.0.0",
            0o555,
        ),
        "bin/spark_diagctl.py": ("spark_diagctl_health", "1.1.0", 0o555),
        "bin/reset_reason_reporter.py": ("reset_reason_reporter", "1.1.0", 0o555),
        "bin/common/asset_id.py": ("support", None, 0o444),
        "bin/common/cli_base.py": ("support", None, 0o444),
        "bin/common/output.py": ("support", None, 0o444),
        "bin/common/__init__.py": ("support", None, 0o444),
        "LICENSE": ("license", "MIT", 0o444),
    }
    bundle = tmp_path / "nvidia.zip"
    member_bytes: dict[str, bytes] = {}
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in installed_contract:
            value = ("fixture:" + path + "\n").encode()
            member_bytes[path] = value
            info = zipfile.ZipInfo(path.replace("/", "\\"))
            info.create_system = 0
            archive.writestr(info, value)
    bundle.chmod(0o644)
    provenance = {
        "artifact": "fixture-nvidia.zip",
        "sha256": _sha(bundle),
        "source": "https://fixture.invalid/nvidia.zip",
        "version": "0.1.0",
    }
    with zipfile.ZipFile(bundle) as archive:
        members = {
            info.filename.replace("\\", "/"): {
                "archive_mode": (info.external_attr >> 16) & 0xFFFF,
                "compressed_size": info.compress_size,
                "compression": info.compress_type,
                "crc32": f"{info.CRC:08x}",
                "kind": "file",
                "size": info.file_size,
            }
            for info in archive.infolist()
        }
    installed = {}
    for path, (role, version, mode) in installed_contract.items():
        entry = {
            "mode": mode,
            "role": role,
            "sha256": hashlib.sha256(member_bytes[path]).hexdigest(),
            "size": len(member_bytes[path]),
        }
        if version is not None:
            entry["version"] = version
        installed[path] = entry
    fake_lock_document = {
        "archive_members": members,
        "filename": provenance["artifact"],
        "installed_files": installed,
        "license": {
            "path": "LICENSE",
            "sha256": installed["LICENSE"]["sha256"],
            "spdx": "MIT",
        },
        "provenance": provenance,
        "provenance_sha256": hashlib.sha256(_canonical(provenance)).hexdigest(),
        "schema_version": 1,
        "sha256": _sha(bundle),
        "source": provenance["source"],
        "version": "0.1.0",
    }
    fake_lock = _write(
        tmp_path / "nvidia-lock.json", _canonical(fake_lock_document), 0o644
    )
    host = tmp_path / "host"
    environment = {
        **os.environ,
        "DGX_INSTALL_TEST_ROOT": str(host),
        "DGX_INSTALL_NVIDIA_LOCK_TEST": str(fake_lock),
        "DGX_INSTALL_LOCK_TEST": str(tmp_path / "install.lock"),
    }
    arguments = [
        "--node-id",
        "spk_0123456789abcdef0123456789abcdef",
        "--agent-artifact",
        str(agent),
        "--agent-sha256",
        _sha(agent),
        "--oras",
        str(oras),
        "--oras-sha256",
        _sha(oras),
        "--nvidia-bundle",
        str(bundle),
        "--health-collector",
        str(health),
        "--health-collector-sha256",
        _sha(health),
        "--site-config",
        str(site),
        "--ca",
        str(ca),
        "--tuf-root",
        str(tuf),
        "--tuf-root-sha256",
        _sha(tuf),
        "--registry-auth",
        str(auth),
        "--enrollment-token",
        str(token),
    ]
    return {
        "arguments": arguments,
        "environment": environment,
        "host": host,
        "lock": fake_lock_document,
        "paths": {
            "agent": agent,
            "oras": oras,
            "bundle": bundle,
            "health": health,
            "site": site,
            "ca": ca,
            "ca_key": ca_key,
            "tuf": tuf,
            "auth": auth,
            "token": token,
        },
    }


def _run_installer(inputs: dict[str, object], arguments: list[str] | None = None):
    return subprocess.run(
        [str(INSTALLER), *(arguments or inputs["arguments"])],
        env=inputs["environment"],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_install_is_idempotent_generic_and_retains_license_provenance(
    installer_inputs: dict[str, object],
) -> None:
    first = _run_installer(installer_inputs)
    second = _run_installer(installer_inputs)

    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    assert json.loads(first.stdout)["status"] == "changed"
    assert json.loads(second.stdout)["status"] == "unchanged"
    host = installer_inputs["host"]
    lock = installer_inputs["lock"]
    nvidia_root = host / "opt/dgx-forge/third-party/nvidia" / lock["sha256"]
    assert (nvidia_root / "LICENSE").read_bytes().startswith(b"fixture:LICENSE")
    source = nvidia_root / "SOURCE.json"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == lock["provenance_sha256"]
    policy = json.loads((host / "etc/dgx-forge-agent/nvidia-policy.json").read_text())
    assert policy["bundle_root"].endswith(lock["sha256"])
    assert {tool["name"] for tool in policy["tools"]} == {
        "device_identity",
        "hardware_config",
        "firmware_reporter",
        "os_build_identity",
        "driver_inventory_reporter",
        "spark_diagctl_health",
        "reset_reason_reporter",
    }
    config = json.loads((host / "etc/dgx-forge-agent/config.json").read_text())
    assert config["node_id"] == "spk_0123456789abcdef0123456789abcdef"
    runtime = json.loads((host / "etc/dgx-forge-agent/runtime-policy.json").read_text())
    assert runtime["release_root"] == str(host / "var/lib/dgx-forge/releases")
    assert runtime["staging_root"] == str(host / "var/lib/dgx-forge/release-staging")
    assert (host / "var/lib/dgx-forge/releases").stat().st_mode & 0o777 == 0o700
    assert (host / "var/lib/dgx-forge/release-staging").stat().st_mode & 0o777 == 0o700


def test_reinstall_restores_token_without_durable_node_bound_active_identity(
    installer_inputs: dict[str, object],
) -> None:
    first = _run_installer(installer_inputs)
    assert first.returncode == 0, first.stderr
    host = installer_inputs["host"]
    installed_token = host / "var/lib/dgx-forge-agent/bootstrap/enrollment-token"
    installed_token.unlink()

    second = _run_installer(installer_inputs)

    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["status"] == "changed"
    assert (
        installed_token.read_bytes() == installer_inputs["paths"]["token"].read_bytes()
    )


def test_reinstall_suppresses_token_only_for_durable_node_bound_active_identity(
    installer_inputs: dict[str, object],
) -> None:
    first = _run_installer(installer_inputs)
    assert first.returncode == 0, first.stderr
    host = installer_inputs["host"]
    installed_token = host / "var/lib/dgx-forge-agent/bootstrap/enrollment-token"
    credentials = host / "var/lib/dgx-forge-agent/credentials"
    generation = credentials / "generation-00000001"
    generation.mkdir(parents=True, mode=0o700)
    generation.chmod(0o700)
    _write(credentials / "active.json", b'{"generation":1}', 0o600)
    _write(
        generation / "credential.json",
        b'{"generation":1,"node_id":"spk_0123456789abcdef0123456789abcdef"}',
        0o600,
    )
    installed_token.unlink()

    second = _run_installer(installer_inputs)

    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["status"] == "unchanged"
    assert not installed_token.exists()


def test_private_key_or_mixed_ca_input_is_rejected_before_target_mutation(
    installer_inputs: dict[str, object],
) -> None:
    ca = installer_inputs["paths"]["ca"]
    key = installer_inputs["paths"]["ca_key"]
    ca.write_bytes(key.read_bytes())
    ca.chmod(0o644)

    rejected = _run_installer(installer_inputs)

    assert rejected.returncode != 0
    assert "public CA bundle" in rejected.stderr
    assert not installer_inputs["host"].exists()


def test_file_publication_resists_parent_and_temporary_inode_substitution(
    installer_inputs: dict[str, object],
) -> None:
    for race in ("replace-temp", "replace-parent"):
        host = installer_inputs["host"]
        environment = dict(installer_inputs["environment"])
        environment.update(
            {
                "DGX_INSTALL_RACE_TEST": race,
                "DGX_INSTALL_RACE_TARGET": "registry-auth.json",
            }
        )
        raced = _run_installer({**installer_inputs, "environment": environment})

        assert raced.returncode != 0
        attacker = host / "var/lib/.dgx-forge-agent.race-attacker"
        if attacker.exists():
            assert not list(attacker.iterdir())
        installed_auth = host / "var/lib/dgx-forge-agent/registry-auth.json"
        assert not installed_auth.exists() or installed_auth.read_bytes() != (
            b"attacker replacement"
        )
        if race == "replace-temp":
            recovered = _run_installer(installer_inputs)
            assert recovered.returncode == 0, recovered.stderr
        else:
            break


@pytest.mark.parametrize(
    "stage",
    ["create", "write", "file-fsync", "tree-fsync", "rename", "parent-fsync"],
)
def test_abandoned_publication_crash_boundaries_recover_bounded_exact_staging(
    installer_inputs: dict[str, object], stage: str
) -> None:
    environment = dict(installer_inputs["environment"])
    environment.update(
        {"DGX_INSTALL_CRASH_AFTER": stage, "DGX_INSTALL_CRASH_TARGET": "A"}
    )

    crashed = _run_installer({**installer_inputs, "environment": environment})
    recovered = _run_installer(installer_inputs)

    assert crashed.returncode == 99
    assert recovered.returncode == 0, recovered.stderr
    assert not list(installer_inputs["host"].rglob("*.new"))


def test_missing_unit_fails_before_target_mutation_and_both_units_are_enabled(
    installer_inputs: dict[str, object], tmp_path: Path
) -> None:
    source = tmp_path / "incomplete-source"
    (source / "agent/supervisor").mkdir(parents=True)
    (source / "agent/systemd").mkdir(parents=True)
    shutil.copy2(
        ROOT / "agent/supervisor/dgx-agent-supervisor", source / "agent/supervisor"
    )
    shutil.copy2(
        ROOT / "agent/systemd/dgx-forge-agent.service", source / "agent/systemd"
    )
    incomplete_environment = dict(installer_inputs["environment"])
    incomplete_environment["DGX_INSTALL_SOURCE_ROOT_TEST"] = str(source)

    missing = _run_installer(
        {**installer_inputs, "environment": incomplete_environment}
    )

    assert missing.returncode != 0
    assert not installer_inputs["host"].exists()

    actions = tmp_path / "systemctl-actions"
    fake_systemctl = tmp_path / "systemctl"
    fake_systemctl.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$DGX_TEST_SYSTEMCTL_ACTIONS"\n'
    )
    fake_systemctl.chmod(0o755)
    complete_environment = dict(installer_inputs["environment"])
    complete_environment.update(
        {
            "DGX_INSTALL_SYSTEMCTL_TEST": str(fake_systemctl),
            "DGX_TEST_SYSTEMCTL_ACTIONS": str(actions),
        }
    )
    installed = _run_installer(
        {**installer_inputs, "environment": complete_environment}
    )

    assert installed.returncode == 0, installed.stderr
    assert actions.read_text().splitlines() == [
        "daemon-reload",
        "enable dgx-forge-agent.service dgx-forge-agent-supervisor.service",
        "start dgx-forge-agent-supervisor.service",
    ]


def test_account_contract_rejects_root_wrong_home_group_and_admin_membership() -> None:
    namespace = runpy.run_path(str(INSTALLER))
    validate = namespace["_validate_service_account"]
    valid = pwd.struct_passwd(
        (
            "dgx-agent",
            "x",
            998,
            998,
            "",
            "/var/lib/dgx-forge-agent",
            "/usr/sbin/nologin",
        )
    )
    primary = grp.struct_group(("dgx-agent", "x", 998, []))
    assert validate(valid, primary, {"dgx-agent"}) == (998, 998)

    invalid = [
        pwd.struct_passwd(("dgx-agent", "x", 0, 998, "", valid.pw_dir, valid.pw_shell)),
        pwd.struct_passwd(("dgx-agent", "x", 998, 998, "", "/tmp", valid.pw_shell)),
        pwd.struct_passwd(
            ("dgx-agent", "x", 998, 997, "", valid.pw_dir, valid.pw_shell)
        ),
    ]
    for account in invalid:
        with pytest.raises(namespace["InstallError"]):
            validate(account, primary, {"dgx-agent"})
    with pytest.raises(namespace["InstallError"]):
        validate(valid, primary, {"dgx-agent", "docker"})


def test_installer_locks_before_account_resolution_and_concurrent_first_install(
    installer_inputs: dict[str, object],
) -> None:
    source = INSTALLER.read_text()
    assert source.index("fcntl.flock(lock_fd") < source.index(
        "service_owner = _service_identity()"
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _run_installer(installer_inputs), range(2)))
    assert sorted(result.returncode for result in results) in ([0, 1], [0, 0])
    assert _run_installer(installer_inputs).returncode == 0


def test_reinstall_rejects_unexpected_symlink_inside_immutable_tree(
    installer_inputs: dict[str, object],
) -> None:
    first = _run_installer(installer_inputs)
    assert first.returncode == 0, first.stderr
    host = installer_inputs["host"]
    lock = installer_inputs["lock"]
    nvidia_root = host / "opt/dgx-forge/third-party/nvidia" / lock["sha256"]
    (nvidia_root / "unexpected-link").symlink_to("/tmp")

    second = _run_installer(installer_inputs)

    assert second.returncode != 0


def test_distinct_explicit_node_ids_generate_distinct_configs(
    installer_inputs: dict[str, object], tmp_path: Path
) -> None:
    first = _run_installer(installer_inputs)
    assert first.returncode == 0, first.stderr
    second_root = tmp_path / "second-host"
    environment = dict(installer_inputs["environment"])
    environment["DGX_INSTALL_TEST_ROOT"] = str(second_root)
    second_inputs = {**installer_inputs, "environment": environment}
    arguments = list(installer_inputs["arguments"])
    arguments[1] = "spk_fedcba9876543210fedcba9876543210"

    second = _run_installer(second_inputs, arguments)

    assert second.returncode == 0, second.stderr
    first_config = json.loads(
        (installer_inputs["host"] / "etc/dgx-forge-agent/config.json").read_text()
    )
    second_config = json.loads(
        (second_root / "etc/dgx-forge-agent/config.json").read_text()
    )
    assert first_config["node_id"] != second_config["node_id"]


def test_installer_never_copies_admin_ca_ssh_or_old_node_private_keys(
    installer_inputs: dict[str, object], tmp_path: Path
) -> None:
    secret_values = [
        b"admin-private",
        b"ca-private",
        b"ssh-private",
        b"old-node-private",
    ]
    for index, value in enumerate(secret_values):
        _write(tmp_path / f"unrelated-{index}.key", value, 0o600)

    result = _run_installer(installer_inputs)

    assert result.returncode == 0, result.stderr
    installed_files = [
        path for path in installer_inputs["host"].rglob("*") if path.is_file()
    ]
    installed_bytes = [path.read_bytes() for path in installed_files]
    assert all(
        secret not in value for secret in secret_values for value in installed_bytes
    )


def test_symlink_input_wrong_architecture_and_extra_archive_member_fail_closed(
    installer_inputs: dict[str, object], tmp_path: Path
) -> None:
    arguments = list(installer_inputs["arguments"])
    site_index = arguments.index("--site-config") + 1
    site_link = tmp_path / "site-link.json"
    site_link.symlink_to(arguments[site_index])
    arguments[site_index] = str(site_link)
    linked = _run_installer(installer_inputs, arguments)
    assert linked.returncode != 0
    assert not installer_inputs["host"].exists()

    site = installer_inputs["paths"]["site"]
    site_document = json.loads(site.read_text())
    site_document["architecture"] = (
        "aarch64" if site_document["architecture"] == "x86_64" else "x86_64"
    )
    site.write_bytes(_canonical(site_document))
    wrong_arch = _run_installer(installer_inputs)
    assert wrong_arch.returncode != 0
    assert not installer_inputs["host"].exists()

    site_document["architecture"] = (
        "aarch64" if platform.machine() in {"aarch64", "arm64"} else "x86_64"
    )
    site.write_bytes(_canonical(site_document))
    bundle = installer_inputs["paths"]["bundle"]
    with zipfile.ZipFile(bundle, "a") as archive:
        archive.writestr("extra", b"extra")
    lock = installer_inputs["lock"]
    lock["sha256"] = _sha(bundle)
    fake_lock = Path(installer_inputs["environment"]["DGX_INSTALL_NVIDIA_LOCK_TEST"])
    fake_lock.write_bytes(_canonical(lock))
    extra = _run_installer(installer_inputs)
    assert extra.returncode != 0
    assert not installer_inputs["host"].exists()
