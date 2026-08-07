import json
import os
import shutil
import subprocess
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[2]
COLLECTOR = ROOT / "nodes" / "bin" / "collect-health"
SCHEMA_PATH = ROOT / "schemas" / "node-health-raw.schema.json"
PACKAGED_SCHEMA_PATH = ROOT / "src" / "cluster_profiles" / "schemas" / "node-health-raw.schema.json"
FIXTURES = ROOT / "tests" / "fixtures" / "node-health"
FUNCTION_ARGS = (
    "--interface",
    "enp1s0f1np1",
    "--hca",
    "rocep1s0f1",
    "--interface",
    "enP2p1s0f1np1",
    "--hca",
    "roceP2p1s0f1",
)


def _write_command(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body)
    path.chmod(0o755)


@pytest.fixture
def raw_schema():
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture
def run_collector(tmp_path):
    invocation = 0

    def run(
        fixture_name: str,
        *,
        gpu_power: str | None = None,
        gpu_name: str | None = None,
        locale_name: str | None = None,
        systemctl_fails: bool = False,
        cpu_stat_variant: str | None = None,
        compute_processes: str = "",
        compute_query_fails: bool = False,
    ):
        nonlocal invocation
        invocation += 1
        fixture_root = tmp_path / f"fixture-{invocation}"
        shutil.copytree(FIXTURES / fixture_name, fixture_root)

        nvidia_fields = (fixture_root / "commands" / "nvidia-smi.txt").read_text().strip().split(", ")
        if gpu_name is not None:
            nvidia_fields[0] = gpu_name
        if gpu_power is not None:
            nvidia_fields[-1] = gpu_power
        (fixture_root / "commands" / "nvidia-smi.txt").write_text(", ".join(nvidia_fields) + "\n")
        (fixture_root / "commands" / "nvidia-compute.txt").write_text(
            compute_processes
        )
        if compute_query_fails:
            (fixture_root / "commands" / "nvidia-compute-fails").touch()

        if cpu_stat_variant is not None:
            proc_root = fixture_root / "proc"
            stat_lines = (proc_root / "stat").read_text().splitlines()
            stat_lines[0] = (proc_root / f"stat.{cpu_stat_variant}.aggregate").read_text().strip()
            (proc_root / "stat").write_text("\n".join(stat_lines) + "\n")
            sample_lines = (proc_root / "stat.sample2").read_text().splitlines()
            sample_lines[0] = (
                proc_root / f"stat.{cpu_stat_variant}.sample2.aggregate"
            ).read_text().strip()
            (proc_root / "stat.sample2").write_text("\n".join(sample_lines) + "\n")

        bin_dir = fixture_root / "bin"
        bin_dir.mkdir()
        command_root = fixture_root / "commands"
        _write_command(bin_dir / "hostname", 'cat "$NODE_HEALTH_COMMAND_ROOT/hostname.txt"\n')
        _write_command(bin_dir / "date", 'cat "$NODE_HEALTH_COMMAND_ROOT/captured-at.txt"\n')
        _write_command(bin_dir / "findmnt", 'cat "$NODE_HEALTH_COMMAND_ROOT/findmnt.txt"\n')
        _write_command(
            bin_dir / "nvidia-smi",
            '''case "$1" in
  --query-gpu=*) cat "$NODE_HEALTH_COMMAND_ROOT/nvidia-smi.txt" ;;
  --query-compute-apps=pid)
    [[ ! -e "$NODE_HEALTH_COMMAND_ROOT/nvidia-compute-fails" ]] || exit 1
    cat "$NODE_HEALTH_COMMAND_ROOT/nvidia-compute.txt"
    ;;
  *) exit 64 ;;
esac
''',
        )
        _write_command(
            bin_dir / "rdma",
            """case "$*" in
  "-j link show") cat "$NODE_HEALTH_COMMAND_ROOT/rdma-link.json" ;;
  "statistic show") cat "$NODE_HEALTH_COMMAND_ROOT/rdma-statistic.txt" ;;
  *) exit 64 ;;
esac
""",
        )
        _write_command(
            bin_dir / "docker",
            """[[ "$*" == "version --format {{.Server.Version}}" ]] || exit 64
cat "$NODE_HEALTH_COMMAND_ROOT/docker-version.txt"
""",
        )
        if systemctl_fails:
            _write_command(bin_dir / "systemctl", "exit 1\n")
        else:
            _write_command(
                bin_dir / "systemctl",
                """case "$*" in
  "show earlyoom --property=LoadState --value") cat "$NODE_HEALTH_COMMAND_ROOT/earlyoom-load.txt" ;;
  "is-enabled earlyoom") cat "$NODE_HEALTH_COMMAND_ROOT/earlyoom-enabled.txt" ;;
  "is-active earlyoom") cat "$NODE_HEALTH_COMMAND_ROOT/earlyoom-active.txt" ;;
  *) exit 64 ;;
esac
""",
            )
        _write_command(
            bin_dir / "sleep",
            'cp "$NODE_HEALTH_PROC_ROOT/stat.sample2" "$NODE_HEALTH_PROC_ROOT/stat"\n',
        )
        if locale_name is not None:
            _write_command(
                bin_dir / "awk",
                """[[ "${LC_ALL:-}" == "C" ]] || exit 65
exec /usr/bin/awk "$@"
""",
            )

        proc_root = fixture_root / "proc"
        completed = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; shift; collect_health "$@"',
                "collect-health-test",
                str(COLLECTOR),
                str(proc_root),
                str(fixture_root / "sys"),
                "250",
                *FUNCTION_ARGS,
            ],
            check=True,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "NODE_HEALTH_COMMAND_ROOT": str(command_root),
                "NODE_HEALTH_PROC_ROOT": str(proc_root),
                **({"LC_ALL": locale_name} if locale_name is not None else {}),
            },
        )
        assert len(completed.stdout.encode()) <= 262144
        return json.loads(completed.stdout)

    return run


def test_collector_reports_cpu_unified_memory_and_root(run_collector):
    """Catches stale CPU sampling or incorrect byte and mount arithmetic."""
    result = run_collector("healthy")

    assert result["cpu"] == {
        "logical_processors": 20,
        "utilization_percent": 50,
        "load_1": 1.25,
        "load_5": 1,
        "load_15": 0.75,
    }
    assert result["memory"] == {
        "total_bytes": 130663231488,
        "available_bytes": 120000000000,
        "used_bytes": 10663231488,
        "used_percent": 8.2,
    }
    assert result["swap"] == {
        "total_bytes": 2147483648,
        "free_bytes": 1073741824,
        "used_bytes": 1073741824,
        "used_percent": 50,
    }
    assert result["root_filesystem"] == {
        "total_bytes": 4031871553536,
        "available_bytes": 3787009835008,
        "used_bytes": 244861718528,
        "used_percent": 6.1,
        "read_only": False,
    }


def test_collector_forces_c_numeric_locale(run_collector):
    """Catches locale-specific decimal commas producing invalid JSON numbers."""
    result = run_collector("healthy", locale_name="nl_NL.UTF-8")

    assert result["cpu"]["utilization_percent"] == 50
    assert result["memory"]["used_percent"] == 8.2
    assert result["thermal_zones"][0]["temperature_c"] == 85


def test_cpu_utilization_does_not_double_count_guest_time(run_collector):
    """Catches Linux guest counters being added twice to total CPU time."""
    result = run_collector("healthy", cpu_stat_variant="guest")

    assert result["cpu"]["utilization_percent"] == 73.3


def test_identity_is_read_from_explicit_fixture_roots(run_collector):
    """Catches a sourced collector that accidentally reads the developer host."""
    result = run_collector("healthy")

    assert result["captured_at"] == "2026-08-02T12:00:00Z"
    assert result["identity"] == {
        "hostname": "spark-3542",
        "boot_id": "11111111-2222-3333-4444-555555555555",
        "uptime_seconds": 12345,
    }


def test_unsupported_power_is_null_not_zero(run_collector):
    """Catches unsupported NVIDIA fields being silently converted to zero."""
    result = run_collector("healthy", gpu_power="N/A")

    assert result["accelerator"]["available"] is True
    assert result["accelerator"]["power_watts"] is None


def test_accelerator_values_are_numeric_when_supported(run_collector):
    """Catches CSV parsing that leaves numeric telemetry as strings."""
    result = run_collector("healthy")

    assert result["accelerator"] == {
        "available": True,
        "name": "NVIDIA GB10",
        "driver_version": "580.173.02",
        "utilization_percent": 12,
        "temperature_c": 41,
        "performance_state": "P8",
        "power_watts": 4.25,
        "active_nvidia_compute_processes": 0,
    }


def test_accelerator_reports_only_bounded_unique_active_compute_count(
    run_collector,
) -> None:
    result = run_collector(
        "healthy", compute_processes=" 441\n442\n441\n"
    )

    assert result["accelerator"]["active_nvidia_compute_processes"] == 2
    assert "441" not in json.dumps(result)
    assert "442" not in json.dumps(result)


@pytest.mark.parametrize(
    "arguments",
    ({"compute_query_fails": True}, {"compute_processes": "not-a-pid\n"}),
)
def test_accelerator_compute_count_is_unknown_when_query_is_ambiguous(
    run_collector, arguments: dict[str, object]
) -> None:
    result = run_collector("healthy", **arguments)

    assert result["accelerator"]["active_nvidia_compute_processes"] is None


def test_thermal_collection_retains_enabled_trip_points(run_collector):
    """Catches disabled trips or reached-state comparisons being reported incorrectly."""
    result = run_collector("healthy")

    assert result["thermal_zones"] == [
        {
            "zone": "thermal_zone0",
            "type": "cpu-thermal",
            "temperature_c": 85,
            "trip_points": [
                {"type": "critical", "temperature_c": 85, "reached": True},
                {"type": "hot", "temperature_c": 90, "reached": False},
                {"type": "passive", "temperature_c": 80, "reached": True},
            ],
        }
    ]


def test_requested_fabric_mapping_and_counters_are_retained(run_collector):
    """Catches argument-order loss, RDMA remapping, or missing monitored counters."""
    result = run_collector("healthy")
    functions = result["fabric"]["functions"]

    assert [item["interface"] for item in functions] == ["enp1s0f1np1", "enP2p1s0f1np1"]
    assert [item["hca"] for item in functions] == ["rocep1s0f1", "roceP2p1s0f1"]
    assert functions[0]["rdma_interface"] == "enp1s0f1np1"
    assert functions[0]["rdma_state"] == "ACTIVE"
    assert functions[0]["counters"]["packet_seq_err"] == 2
    assert functions[1]["counters"]["roce_adp_retrans"] == 7
    assert len(functions[0]["counters"]) == 19


def test_services_report_query_availability_and_earlyoom_state(run_collector):
    """Catches Docker enumeration or earlyoom status strings leaking into booleans."""
    result = run_collector("healthy")

    assert result["services"] == {
        "docker_available": True,
        "docker_version": "29.2.1",
        "earlyoom_load_state": "not-found",
        "earlyoom_enabled": False,
        "earlyoom_active": False,
    }


def test_failed_earlyoom_queries_remain_null(run_collector):
    """Catches failed service queries being reported as a known disabled state."""
    result = run_collector("healthy", systemctl_fails=True)

    assert result["services"]["earlyoom_load_state"] is None
    assert result["services"]["earlyoom_enabled"] is None
    assert result["services"]["earlyoom_active"] is None


def test_raw_output_is_bounded_when_a_command_returns_an_oversized_field(run_collector):
    """Catches an unbounded optional command response escaping into SSH output."""
    result = run_collector("healthy", gpu_name="x" * 300_000)

    assert len(json.dumps(result).encode()) <= 262144
    assert result["accelerator"]["available"] is True


def test_raw_output_satisfies_schema(run_collector, raw_schema):
    """Catches collector and controller raw-contract drift."""
    jsonschema.validate(run_collector("healthy"), raw_schema)


def test_packaged_schema_matches_repository_contract(raw_schema):
    """Catches packaging a stale schema copy for the controller."""
    assert json.loads(PACKAGED_SCHEMA_PATH.read_text()) == raw_schema


@pytest.mark.parametrize(
    "arguments",
    [
        (),
        ("--json",),
        ("--json", "--cpu-sample-ms", "250"),
        ("--json", "--cpu-sample-ms", "250", "--interface", "eth0"),
        ("--json", "--cpu-sample-ms", "250", "--hca", "rdma0"),
        ("--json", "--cpu-sample-ms", "250", "--wat", "x"),
        ("--json", "--cpu-sample-ms", "0", "--interface", "eth0", "--hca", "rdma0"),
        (
            "--json",
            "--cpu-sample-ms",
            "250",
            "--interface",
            "eth0",
            "--hca",
            "rdma0",
            "--interface",
            "eth0",
            "--hca",
            "rdma1",
        ),
        (
            "--json",
            "--cpu-sample-ms",
            "250",
            "--interface",
            "../proc",
            "--hca",
            "rdma0",
        ),
    ],
)
def test_collector_rejects_incomplete_unknown_or_unsafe_arguments(arguments):
    """Catches malformed mappings being accepted or turned into arbitrary sysfs paths."""
    completed = subprocess.run(
        ["bash", str(COLLECTOR), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""


def test_collector_rejects_huge_cpu_sample_before_arithmetic():
    """Catches attacker-sized decimal input reaching Bash arithmetic expansion."""
    completed = subprocess.run(
        [
            "bash",
            str(COLLECTOR),
            "--json",
            "--cpu-sample-ms",
            "0" + "9" * 1_000,
            "--interface",
            "eth0",
            "--hca",
            "rdma0",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr.startswith("Usage:")
    assert "value too great for base" not in completed.stderr


def test_collector_streamed_to_bash_dispatches_main_under_nounset():
    """Catches stdin execution dereferencing an unset BASH_SOURCE entry."""
    completed = subprocess.run(
        [
            "bash",
            "-s",
            "--",
            "--json",
            "--cpu-sample-ms",
            "0",
            "--interface",
            "eth0",
            "--hca",
            "rdma0",
        ],
        check=False,
        input=COLLECTOR.read_text(),
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr.startswith("Usage:")
