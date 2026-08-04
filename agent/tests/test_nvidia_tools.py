from __future__ import annotations

import hashlib
import json
import os
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from dgx_agent import nvidia_tools
from dgx_agent.nvidia_tools import (
    NVIDIA_TOOL_NAMES,
    REVIEWED_BUNDLE_SHA256,
    REVIEWED_BUNDLE_VERSION,
    InstalledPolicy,
    InstalledPolicyError,
    InstalledToolSecurityError,
    ToolName,
    normalize_tool_document,
    open_verified_executable,
    verify_reviewed_support_files,
)
from dgx_agent_protocol import canonical_message

TOOL_CONTRACT = {
    "device_identity": ("bin/device_identity.py", "1.1.0"),
    "hardware_config": ("bin/hardware_config.py", "1.0.0"),
    "firmware_reporter": ("bin/firmware_reporter.py", "1.0.0"),
    "os_build_identity": ("bin/os_build_identity.py", "1.0.0"),
    "driver_inventory_reporter": ("bin/driver_inventory_reporter.py", "1.0.0"),
    "spark_diagctl_health": ("bin/spark_diagctl.py", "1.1.0"),
    "reset_reason_reporter": ("bin/reset_reason_reporter.py", "1.1.0"),
}
COMMON_ARGUMENTS = ["--stdout-json", "--no-write-file", "--quiet"]
TOOL_DIGESTS = {
    "device_identity": "110acb65e54092a63d93f8d0448855717323c7251bbaf661a7d6cb41836f2dcf",
    "hardware_config": "07c05c03f65e9b707bc18ebd2ec010ac1622701fa0b87858014a5b71fd1af5bb",
    "firmware_reporter": "c5887cb8b456295ea937a44cf05d8c1a3fa64b2ac8239f35be61e8deb358d387",
    "os_build_identity": "ee2f06d7ae25438ed0a7258eeeecdde76dba24c5c82f9dec510c361b9d75f6f9",
    "driver_inventory_reporter": "f5f90c05f077f1cd6fa387d1f6eac3b7f40b7d859c6e5886c73ec03629fdfc26",
    "spark_diagctl_health": "03de23664d3a24295ce605075be957328f47c24fa37afb7bbfe60988cbee42c2",
    "reset_reason_reporter": "212b49f894e4703cc85743217a0a9d9f2bb5891702266df84b907df960d83774",
}
SUPPORT_CONTRACT = {
    "bin/common/asset_id.py": ("35277c9d42c97960434f10e7f8dfda0a7e12cfbe00aec0d86ea88099c5ac9eca", 8072),
    "bin/common/cli_base.py": ("0b1f72a2056cbb5a3c717e7853b7f4d986a4b91b7920eadab68888b101f1b1da", 15147),
    "bin/common/output.py": ("6938255c277aa5b3b2e805a2cbfdc52d86c5d19910591cb42272a7eb280e2426", 9200),
    "bin/common/__init__.py": ("a3b4329f7500a2f9d95369ba32b3eb563c27a76d6d96d9f98dac1c1fc41b938a", 754),
}


def _executable(path: Path, body: bytes = b"#!/bin/sh\nexit 0\n") -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(body)
    path.chmod(0o755)
    return hashlib.sha256(body).hexdigest()


def policy_document(tmp_path: Path) -> tuple[dict[str, object], Path]:
    root = tmp_path / "bundle"
    root.mkdir(mode=0o700, exist_ok=True)
    tools = []
    for name in NVIDIA_TOOL_NAMES:
        relative_path, version = TOOL_CONTRACT[name]
        path = root / relative_path
        _executable(path)
        arguments = [*COMMON_ARGUMENTS, "health"] if name == "spark_diagctl_health" else COMMON_ARGUMENTS
        tools.append(
            {
                "name": name,
                "version": version,
                "executable": str(path),
                "sha256": TOOL_DIGESTS[name],
                "arguments": arguments,
                "timeout_seconds": 2,
                "output_limit_bytes": 8192,
            }
        )
    collector = tmp_path / "libexec" / "collect-health"
    collector_digest = _executable(collector)
    support_files = []
    for relative, (digest, size) in SUPPORT_CONTRACT.items():
        support = root / relative
        _executable(support, f"# fixture {relative}\n".encode())
        support.chmod(0o644)
        support_files.append({"relative_path": relative, "sha256": digest, "size_bytes": size})
    document: dict[str, object] = {
        "schema_version": 1,
        "bundle_version": REVIEWED_BUNDLE_VERSION,
        "bundle_sha256": REVIEWED_BUNDLE_SHA256,
        "bundle_root": str(root),
        "tools": tools,
        "support_files": support_files,
        "health": {
            "executable": str(collector),
            "sha256": collector_digest,
            "cpu_sample_ms": 250,
            "fabric_pairs": [
                {"interface": "enp1s0f1np1", "hca": "rocep1s0f1"},
                {"interface": "enP2p1s0f1np1", "hca": "roceP2p1s0f1"},
            ],
            "timeout_seconds": 5,
            "output_limit_bytes": 131072,
        },
    }
    return document, root


def fixture_policy(policy: InstalledPolicy) -> InstalledPolicy:
    tools = tuple(
        replace(tool, sha256=hashlib.sha256(tool.executable.read_bytes()).hexdigest())
        for tool in policy.tools
    )
    support_files = tuple(
        replace(
            item,
            sha256=hashlib.sha256((policy.bundle_root / item.relative_path).read_bytes()).hexdigest(),
            size_bytes=(policy.bundle_root / item.relative_path).stat().st_size,
        )
        for item in policy.support_files
    )
    return replace(policy, tools=tools, support_files=support_files)


def write_policy(tmp_path: Path, document: object, *, raw: bytes | None = None) -> Path:
    path = tmp_path / "installed-policy.json"
    path.write_bytes(raw if raw is not None else json.dumps(document).encode())
    path.chmod(0o644)
    return path


def test_installed_policy_contract_is_strict_typed_and_immutable(tmp_path) -> None:
    document, root = policy_document(tmp_path)

    policy = InstalledPolicy._load_for_test(write_policy(tmp_path, document))

    assert policy.schema_version == 1
    assert policy.bundle_version == "0.1.0"
    assert policy.bundle_sha256 == "0eb1c93dd839b6bd4136cc8b79ea04a1e44fd637ff6afa6ee9568951a4c179f3"
    assert policy.bundle_root == root
    assert tuple(item.name.value for item in policy.tools) == NVIDIA_TOOL_NAMES
    assert policy.tools[-2].name is ToolName.SPARK_DIAGCTL_HEALTH
    assert policy.tools[-2].version == "1.1.0"
    assert policy.tools[-2].executable == root / "bin/spark_diagctl.py"
    assert policy.tools[-2].arguments == (
        "--stdout-json",
        "--no-write-file",
        "--quiet",
        "health",
    )
    assert policy.health.cpu_sample_ms == 250
    assert tuple(item.relative_path for item in policy.support_files) == tuple(sorted(SUPPORT_CONTRACT))
    assert policy.tools[0].sha256 == "110acb65e54092a63d93f8d0448855717323c7251bbaf661a7d6cb41836f2dcf"
    assert policy.health.fabric_pairs[0].interface == "enp1s0f1np1"
    with pytest.raises(AttributeError):
        policy.bundle_version = "changed"  # type: ignore[misc]


def test_production_policy_rejects_unprivileged_mutable_ownership(tmp_path) -> None:
    document, _ = policy_document(tmp_path)
    path = write_policy(tmp_path, document)

    with pytest.raises(InstalledToolSecurityError):
        InstalledPolicy.load(path)

    assert InstalledPolicy._load_for_test(path).bundle_version == "0.1.0"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.pop("bundle_version"),
        lambda value: value.update({"extra": True}),
        lambda value: value.update({"schema_version": 2}),
        lambda value: value.update({"bundle_version": "latest"}),
        lambda value: value.update({"bundle_sha256": "f" * 64}),
        lambda value: value["tools"][0].pop("version"),
        lambda value: value["tools"][0].update({"version": "latest"}),
        lambda value: value.pop("support_files"),
        lambda value: value["support_files"][0].update({"sha256": "f" * 64}),
        lambda value: value["tools"][0].update({"command": ["id"]}),
        lambda value: value["health"].update({"environment": {"X": "y"}}),
    ],
    ids=[
        "missing",
        "unknown",
        "schema",
        "bundle-version",
        "bundle-digest",
        "missing-tool-version",
        "wrong-tool-version",
        "missing-support-files",
        "wrong-support-digest",
        "tool-unknown",
        "health-unknown",
    ],
)
def test_policy_rejects_missing_unknown_or_unreviewed_values(tmp_path, mutation) -> None:
    document, _ = policy_document(tmp_path)
    mutation(document)

    with pytest.raises(InstalledPolicyError):
        InstalledPolicy._load_for_test(write_policy(tmp_path, document))


def test_policy_rejects_duplicate_json_fields_tools_and_fabric_pairs(tmp_path) -> None:
    document, _ = policy_document(tmp_path)
    encoded = json.dumps(document).encode()
    duplicate = encoded[:-1] + b',"schema_version":1}'
    with pytest.raises(InstalledPolicyError, match="duplicate"):
        InstalledPolicy._load_for_test(write_policy(tmp_path, {}, raw=duplicate))

    document, _ = policy_document(tmp_path)
    document["tools"][1]["name"] = document["tools"][0]["name"]
    with pytest.raises(InstalledPolicyError):
        InstalledPolicy._load_for_test(write_policy(tmp_path, document))

    document, _ = policy_document(tmp_path)
    document["health"]["fabric_pairs"].append(
        {"interface": "enp1s0f1np1", "hca": "different"}
    )
    with pytest.raises(InstalledPolicyError):
        InstalledPolicy._load_for_test(write_policy(tmp_path, document))


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("root", "bundle_root", "relative/root"),
        ("root", "bundle_root", "/opt/dgx/../escape"),
        ("tool", "executable", "device_identity"),
        ("tool", "executable", "/tmp/not-inside-bundle"),
        ("tool", "sha256", "A" * 64),
        ("tool", "arguments", [*COMMON_ARGUMENTS, "../secret"]),
        ("tool", "arguments", [*COMMON_ARGUMENTS, "x" * 257]),
        ("tool", "timeout_seconds", 0),
        ("tool", "timeout_seconds", 16),
        ("tool", "output_limit_bytes", 0),
        ("tool", "output_limit_bytes", 262145),
        ("health", "cpu_sample_ms", 0),
        ("health", "cpu_sample_ms", 10001),
    ],
)
def test_policy_rejects_unsafe_paths_arguments_and_bounds(
    tmp_path, section, field, value
) -> None:
    document, _ = policy_document(tmp_path)
    if section == "root":
        document[field] = value
    elif section == "tool":
        document["tools"][0][field] = value
    else:
        document["health"][field] = value

    with pytest.raises(InstalledPolicyError):
        InstalledPolicy._load_for_test(write_policy(tmp_path, document))


def test_policy_file_read_is_bounded_utf8_regular_and_descriptor_safe(tmp_path) -> None:
    document, _ = policy_document(tmp_path)
    real = write_policy(tmp_path, document)
    linked = tmp_path / "linked-policy.json"
    linked.symlink_to(real)
    with pytest.raises(InstalledPolicyError):
        InstalledPolicy._load_for_test(linked)

    fifo = tmp_path / "policy.fifo"
    os.mkfifo(fifo)
    with pytest.raises(InstalledPolicyError):
        InstalledPolicy._load_for_test(fifo)

    with pytest.raises(InstalledPolicyError):
        InstalledPolicy._load_for_test(write_policy(tmp_path, {}, raw=b"\xff"))
    with pytest.raises(InstalledPolicyError, match="large"):
        InstalledPolicy._load_for_test(write_policy(tmp_path, {}, raw=b"{" + b" " * 65536 + b"}"))


def test_present_executables_are_descriptor_verified_for_type_mode_owner_and_digest(
    tmp_path, monkeypatch
) -> None:
    document, root = policy_document(tmp_path)
    policy = fixture_policy(InstalledPolicy._load_for_test(write_policy(tmp_path, document)))
    policy.verify_installed()

    first = Path(document["tools"][0]["executable"])
    first.write_bytes(b"tampered")
    with pytest.raises(InstalledToolSecurityError, match="digest"):
        policy.verify_installed()

    first.unlink()
    first.symlink_to(root / TOOL_CONTRACT[NVIDIA_TOOL_NAMES[1]][0])
    with pytest.raises(InstalledToolSecurityError):
        policy.verify_installed()

    first.unlink()
    os.mkfifo(first)
    with pytest.raises(InstalledToolSecurityError):
        policy.verify_installed()

    first.unlink()
    _executable(first)
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(InstalledToolSecurityError):
        policy.verify_installed()

    with pytest.raises(InstalledToolSecurityError):
        open_verified_executable(Path("/dev/null"), hashlib.sha256(b"").hexdigest())


def test_missing_tool_is_unavailable_but_present_tampering_is_not(tmp_path) -> None:
    document, _ = policy_document(tmp_path)
    policy = fixture_policy(InstalledPolicy._load_for_test(write_policy(tmp_path, document)))
    missing = Path(document["tools"][0]["executable"])
    missing.unlink()

    availability = policy.verify_installed()
    assert availability[ToolName.DEVICE_IDENTITY] is False
    assert availability[ToolName.HARDWARE_CONFIG] is True

    present = Path(document["tools"][1]["executable"])
    present.chmod(0o777)
    with pytest.raises(InstalledToolSecurityError):
        policy.verify_installed()


def test_rejected_bundle_directory_does_not_leak_descriptors(tmp_path) -> None:
    document, root = policy_document(tmp_path)
    policy = fixture_policy(InstalledPolicy._load_for_test(write_policy(tmp_path, document)))
    root.chmod(0o777)
    before = len(os.listdir("/proc/self/fd"))

    for _ in range(10):
        with pytest.raises(InstalledToolSecurityError):
            policy.verify_installed()

    assert len(os.listdir("/proc/self/fd")) == before


def test_rejected_artifact_snapshot_does_not_leak_descriptors(tmp_path) -> None:
    executable = tmp_path / "tool"
    _executable(executable)
    before = len(os.listdir("/proc/self/fd"))

    for _ in range(10):
        with pytest.raises(InstalledToolSecurityError, match="digest"):
            open_verified_executable(
                executable,
                "a" * 64,
                _test_only_allow_unprivileged=True,
            )

    assert len(os.listdir("/proc/self/fd")) == before


def test_unavailable_executable_never_closes_standard_input(tmp_path) -> None:
    try:
        before = os.fstat(0)
    except OSError:
        pytest.skip("test process has no standard input descriptor")

    assert open_verified_executable(
        tmp_path / "missing",
        "a" * 64,
        _test_only_allow_unprivileged=True,
    ) is None
    assert os.fstat(0) == before


@pytest.mark.parametrize("attack", ["tamper", "symlink", "extra"])
def test_imported_common_module_attacks_fail_before_execution(tmp_path, attack) -> None:
    document, _ = policy_document(tmp_path)
    policy = fixture_policy(InstalledPolicy._load_for_test(write_policy(tmp_path, document)))
    target = policy.bundle_root / "bin/common/output.py"
    if attack == "tamper":
        target.write_text("attacker", encoding="utf-8")
    elif attack == "symlink":
        target.unlink()
        target.symlink_to(policy.bundle_root / "bin/common/asset_id.py")
    else:
        (policy.bundle_root / "bin/common/sitecustomize.py").write_text("attacker", encoding="utf-8")

    with pytest.raises(InstalledToolSecurityError):
        verify_reviewed_support_files(policy)


def test_reviewed_support_archive_is_exact_deterministic_and_write_sealed(tmp_path) -> None:
    document, _ = policy_document(tmp_path)
    policy = fixture_policy(InstalledPolicy._load_for_test(write_policy(tmp_path, document)))

    first = nvidia_tools.open_verified_support_archive(policy)
    second = nvidia_tools.open_verified_support_archive(policy)
    try:
        with zipfile.ZipFile(f"/proc/self/fd/{first}") as archive:
            assert archive.namelist() == [
                "__init__.py",
                "asset_id.py",
                "cli_base.py",
                "output.py",
            ]
        assert os.pread(first, 1 << 20, 0) == os.pread(second, 1 << 20, 0)
        with pytest.raises(OSError):
            os.write(first, b"attacker")
    finally:
        os.close(first)
        os.close(second)


def envelope(data, *, ok: bool = True) -> bytes:
    return json.dumps(
        {"ok": ok, "data": data, "errors": [], "meta": {"duration_ms": 3}},
        sort_keys=True,
    ).encode()


@pytest.mark.parametrize(
    ("name", "source", "expected"),
    [
        (
            ToolName.DEVICE_IDENTITY,
            {
                "asset_id": "secret-asset",
                "source": "secret-source",
                "sys_vendor": "NVIDIA",
                "product_name": "NVIDIA DGX Spark",
                "product_version": "1.0",
                "product_serial": "SECRET-SERIAL",
                "uuid": "11111111-2222-3333-4444-555555555555",
                "board_serial": "secret",
                "chassis_serial": "secret",
                "os_machine_id": "secret",
                "platform_dmi": {"product_uuid": "secret"},
            },
            {"product_name": "NVIDIA DGX Spark", "product_version": "1.0", "sys_vendor": "NVIDIA"},
        ),
        (
            ToolName.HARDWARE_CONFIG,
            {
                "platform_dmi": {"sys_vendor": "NVIDIA", "product_name": "DGX Spark", "product_version": "1.0", "product_serial": "secret"},
                "cpu": {"architecture": "aarch64", "logical_cpus": 20, "sockets": 1, "cores_per_socket": 10, "threads_per_core": 2, "model_names": ["NVIDIA Grace"], "max_mhz": 4000, "min_mhz": 800},
                "memory": {"mem_total_bytes": 128000000000, "mem_free_bytes": 8000000000, "mem_available_bytes": 120000000000},
                "storage": [{"name": "secret-device", "type": "disk", "size_bytes": 4000000000000, "model": "NVMe", "tran": "nvme", "rota": False, "serial": "secret", "wwn": "secret"}],
                "network": [{"ifname": "secret0", "mac": "00:11:22:33:44:55", "is_virtual": False, "is_wireless": False, "mtu": 9000, "operstate": "up", "speed_mbps": 200000, "driver": "mlx5_core", "driver_version": "1.2", "firmware_version": "3.4", "pci_vendor_id": "15b3", "pci_device_id": "1021"}],
                "gpu": [{"index": 0, "name": "GB10", "driver_version": "580.173.02", "memory_total_mib": 128000, "uuid": "secret", "pci_bus_id": "secret"}],
                "pci": [{"class_text": "VGA", "vendor_device_id": "10de:2b85", "description": "NVIDIA GPU", "pci_addr": "secret"}],
            },
            {
                "platform": {"product_name": "DGX Spark", "product_version": "1.0", "sys_vendor": "NVIDIA"},
                "cpu": {"architecture": "aarch64", "cores_per_socket": 10, "logical_cpus": 20, "max_mhz": 4000, "min_mhz": 800, "model_names": ("NVIDIA Grace",), "sockets": 1, "threads_per_core": 2},
                "memory": {"mem_available_bytes": 120000000000, "mem_free_bytes": 8000000000, "mem_total_bytes": 128000000000},
                "storage": ({"model": "NVMe", "rota": False, "size_bytes": 4000000000000, "tran": "nvme", "type": "disk"},),
                "network": ({"driver": "mlx5_core", "driver_version": "1.2", "firmware_version": "3.4", "is_virtual": False, "is_wireless": False, "mtu": 9000, "operstate": "up", "pci_device_id": "1021", "pci_vendor_id": "15b3", "speed_mbps": 200000},),
                "gpu": ({"driver_version": "580.173.02", "index": 0, "memory_total_mib": 128000, "name": "GB10"},),
                "pci": ({"class_text": "VGA", "description": "NVIDIA GPU", "vendor_device_id": "10de:2b85"},),
            },
        ),
        (
            ToolName.FIRMWARE_REPORTER,
            {
                "platform_dmi": {"bios_vendor": "NVIDIA", "bios_version": "1.2.3", "bios_date": "2026-01-01", "product_serial": "secret"},
                "fwupd": {"available": True, "fwupdmgr_version": "2.0", "devices": [{"name": "UEFI", "current_version": "1.2.3", "minimum_version": "1.0", "vendor": "NVIDIA", "update_state": "success", "device_id": "secret", "summary": "secret"}]},
                "nics": [{"is_wireless": False, "driver": "mlx5_core", "driver_version": "1.2", "firmware_version": "3.4", "ifname": "secret0", "mac": "secret"}],
                "nvme": [{"model": "NVMe", "firmware_rev": "9.1", "serial": "secret", "controller": "secret"}],
                "gpu": [{"index": 0, "name": "GB10", "driver_version": "580", "vbios_version": "1.0", "gsp_firmware_version": "2.0", "inforom": "3.0", "uuid": "secret"}],
                "pci": [{"class_text": "VGA", "vendor_device_id": "10de:2b85", "description": "GPU", "pci_addr": "secret"}],
            },
            {
                "platform": {"bios_date": "2026-01-01", "bios_vendor": "NVIDIA", "bios_version": "1.2.3"},
                "fwupd": {"available": True, "devices": ({"current_version": "1.2.3", "minimum_version": "1.0", "name": "UEFI", "update_state": "success", "vendor": "NVIDIA"},), "fwupdmgr_version": "2.0"},
                "nics": ({"driver": "mlx5_core", "driver_version": "1.2", "firmware_version": "3.4", "is_wireless": False},),
                "nvme": ({"firmware_rev": "9.1", "model": "NVMe"},),
                "gpu": ({"driver_version": "580", "gsp_firmware_version": "2.0", "index": 0, "inforom": "3.0", "name": "GB10", "vbios_version": "1.0"},),
                "pci": ({"class_text": "VGA", "description": "GPU", "vendor_device_id": "10de:2b85"},),
            },
        ),
        (
            ToolName.OS_BUILD_IDENTITY,
            {
                "os": {"os_release": {"ID": "ubuntu", "VERSION_ID": "24.04", "VERSION": "24.04.3", "PRETTY_NAME": "Ubuntu 24.04", "UBUNTU_CODENAME": "noble", "HOME_URL": "https://secret"}, "kernel": {"uname_r": "6.11.0", "uname_a": "secret raw"}},
                "dgx": {"dgx_release": {"DGX_SWBUILD_VERSION": "7.3.1", "DGX_SWBUILD_DATE": "2026-08-01", "DGX_COMMIT_ID": "abc123", "SOURCE_PATH": "/secret"}},
                "baseline": {"fingerprint_sha256": "a" * 64, "fingerprint_material": "secret", "packages": {"nvidia-driver": "580.173.02"}, "snaps": [{"name": "lxd", "version": "5.0", "rev": "123", "tracking": "latest/stable"}]},
            },
            {
                "os": {"kernel": {"uname_r": "6.11.0"}, "os_release": {"ID": "ubuntu", "PRETTY_NAME": "Ubuntu 24.04", "UBUNTU_CODENAME": "noble", "VERSION": "24.04.3", "VERSION_ID": "24.04"}},
                "dgx": {"dgx_release": {"DGX_COMMIT_ID": "abc123", "DGX_SWBUILD_DATE": "2026-08-01", "DGX_SWBUILD_VERSION": "7.3.1"}},
                "baseline": {"fingerprint_sha256": "a" * 64, "packages": ({"name": "nvidia-driver", "version": "580.173.02"},), "snaps": ({"name": "lxd", "revision": "123", "version": "5.0"},)},
            },
        ),
        (
            ToolName.DRIVER_INVENTORY_REPORTER,
            {"kernel": {"uname_r": "6.11.0", "uname_a": "secret raw"}, "drivers_manifest": [{"module": "nvidia", "modinfo": {"version": "580.173.02", "license": "MIT", "firmware": "gsp.bin", "filename": "/secret", "alias": "secret"}, "used_by": {"net_ifaces": ["secret0"]}}], "gpu": [{"name": "GB10", "driver_version": "580.173.02", "uuid": "secret"}], "nics": [{"driver": "mlx5_core", "driver_version": "1.2", "firmware_version": "3.4", "ifname": "secret"}], "usb": [{"path": "/secret"}]},
            {"kernel": {"uname_r": "6.11.0"}, "drivers_manifest": ({"firmware": "gsp.bin", "license": "MIT", "module": "nvidia", "version": "580.173.02"},), "gpu": ({"driver_version": "580.173.02", "name": "GB10"},), "nics": ({"driver": "mlx5_core", "driver_version": "1.2", "firmware_version": "3.4"},)},
        ),
        (
            ToolName.SPARK_DIAGCTL_HEALTH,
            {
                "cpu": {"load_average": {"1min": 1.0, "5min": 0.8, "15min": 0.5}, "cpu_count": 20, "top_processes": [{"command": "secret"}]},
                "memory": {"mem_total_kb": 125000000, "mem_free_kb": 8000000, "mem_available_kb": 117000000, "mem_used_percent": 6.2},
                "disk": {"filesystems": [{"filesystem": "/dev/secret", "size": "4T", "used": "500G", "avail": "3.5T", "use_percent": "12.5%", "mounted_on": "/secret"}]},
                "network": {"interfaces": [{"name": "secret0", "state": "UP", "rx_bytes": 1000, "tx_bytes": 2000}]},
                "thermal": {"sensors_available": True, "temperatures": ["secret free form"]},
                "gpu": {"nvidia_smi_available": True, "gpus": [{"index": "0", "name": "GB10", "temp_c": "41", "util_gpu_percent": "12", "util_mem_percent": "8", "mem_used_mib": "10", "mem_total_mib": "128000", "power_draw_w": "4.25", "power_limit_w": "100"}]},
            },
            {
                "cpu": {"cpu_count": 20, "load_average": {"1min": 1.0, "15min": 0.5, "5min": 0.8}},
                "memory": {"mem_available_kb": 117000000, "mem_free_kb": 8000000, "mem_total_kb": 125000000, "mem_used_percent": 6.2},
                "disk": {"count": 1, "maximum_used_percent": 12.5},
                "network": {"interface_count": 1, "rx_bytes": 1000, "tx_bytes": 2000, "up_count": 1},
                "thermal": {"sensors_available": True},
                "gpu": {"gpus": ({"index": 0, "mem_total_mib": 128000, "mem_used_mib": 10, "name": "GB10", "power_draw_w": 4.25, "power_limit_w": 100, "temp_c": 41, "util_gpu_percent": 12, "util_mem_percent": 8},), "nvidia_smi_available": True},
            },
        ),
        (
            ToolName.RESET_REASON_REPORTER,
            {"current_boot": {"kernel": "6.11.0", "uptime_seconds": 123, "boot_id": "11111111-2222-3333-4444-555555555555"}, "last_reset": {"reason_code": "power-on", "confidence": 95, "summary": "secret free form", "evidence": ["secret"]}, "signals": ["secret"], "logs": ["secret"]},
            {"current_boot": {"kernel": "6.11.0", "uptime_seconds": 123}, "last_reset": {"confidence": 95, "reason_code": "power-on"}},
        ),
    ],
)
def test_per_tool_allowlists_drop_sensitive_and_unknown_fields(name, source, expected) -> None:
    normalized = normalize_tool_document(name, envelope(source), limit=8192)

    assert normalized == expected
    rendered = canonical_message(normalized).lower()
    for forbidden in (
        b"secret",
        b"serial",
        b"hostname",
        b"machine_id",
        b"username",
        b"uuid",
        b"command_line",
        b"process_name",
        b'"processes"',
        b"log_lines",
        b"journal",
        b"path",
        b"artifact",
        b"unknown",
    ):
        assert forbidden not in rendered


def test_normalization_is_stable_under_reordered_source_objects() -> None:
    first = envelope({"os": {"os_release": {"ID": "ubuntu", "VERSION_ID": "24.04"}, "kernel": {"uname_r": "6.11"}}})
    second = b'{"meta":{},"errors":[],"data":{"os":{"kernel":{"uname_r":"6.11"},"os_release":{"VERSION_ID":"24.04","ID":"ubuntu"}}},"ok":true}'

    expected = b'{"os":{"kernel":{"uname_r":"6.11"},"os_release":{"ID":"ubuntu","VERSION_ID":"24.04"}}}'
    assert canonical_message(normalize_tool_document(ToolName.OS_BUILD_IDENTITY, first, limit=8192)) == expected
    assert canonical_message(normalize_tool_document(ToolName.OS_BUILD_IDENTITY, second, limit=8192)) == expected


@pytest.mark.parametrize(
    "sensitive",
    [
        "11111111-2222-4333-8444-555555555555",
        "{11111111-2222-4333-8444-555555555555}",
        "00:11:22:33:44:55",
        "192.0.2.44",
        "2001:db8::44",
        "/var/log/private",
        r"C:\\private\\file",
    ],
)
def test_sensitive_value_shapes_are_dropped_even_from_allowlisted_fields(sensitive) -> None:
    normalized = normalize_tool_document(
        ToolName.DEVICE_IDENTITY,
        envelope({"sys_vendor": sensitive, "product_name": sensitive, "product_version": sensitive}),
        limit=8192,
    )

    assert normalized == {}


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff",
        b"not-json",
        b"[]",
        b'{"ok":true,"data":{},"errors":[],"meta":{},"extra":1}',
        b'{"ok":true,"ok":false,"data":{},"errors":[],"meta":{}}',
        b'{"ok":"yes","data":{},"errors":[],"meta":{}}',
        b'{"ok":true,"data":null,"errors":[],"meta":{}}',
        b'{"ok":true,"data":{},"errors":"raw secret","meta":{}}',
    ],
)
def test_nvidia_envelope_rejects_malformed_or_incompatible_documents(raw) -> None:
    with pytest.raises(InstalledPolicyError):
        normalize_tool_document(ToolName.DEVICE_IDENTITY, raw, limit=8192)


def test_nvidia_json_input_is_bounded_before_decode() -> None:
    with pytest.raises(InstalledPolicyError, match="large"):
        normalize_tool_document(
            ToolName.DEVICE_IDENTITY,
            b"{" + b" " * 8192 + b"}",
            limit=8192,
        )
