"""Regression tests for the offline parsers used by ``validate-fabric``."""

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "validate_fabric.py"


@pytest.fixture
def validate_module():
    """Load the parser from the executable without running a live check."""
    loader = SourceFileLoader("validate_fabric", str(SCRIPT))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


@pytest.fixture
def parse_nccl(validate_module):
    return validate_module.parse_nccl


def test_rejects_tcp_fallback(parse_nccl):
    """A successful process is not evidence of RDMA if NCCL selected sockets."""
    result = parse_nccl("NET/Socket : Using enp...\nAvg bus bandwidth : 11.0")

    assert result.passed is False


def test_accepts_ib_transport(parse_nccl):
    """The acceptance parser records the selected RDMA transport."""
    result = parse_nccl("NET/IB : Using rocep1s0f1\nAvg bus bandwidth : 20.0")

    assert result.transport == "IB"


def test_rejects_ib_diagnostic_without_a_using_selection(parse_nccl):
    """Discovery output is not evidence that NCCL selected an RDMA transport."""
    result = parse_nccl("NET/IB : No device found\nAvg bus bandwidth : 19.3")

    assert result.passed is False
    assert result.reason == "NCCL did not report NET/IB : Using"


def test_rejects_nccl_output_without_measured_bandwidth(parse_nccl):
    """NCCL initialization alone does not make an all-reduce an acceptance result."""
    result = parse_nccl("NET/IB : Using rocep1s0f1\n# Out of bounds values")

    assert result.passed is False
    assert result.bus_bandwidth_gbps is None


def test_rejects_non_positive_nccl_bandwidth(parse_nccl):
    """A zero/negative metric is not a successful measured benchmark."""
    result = parse_nccl("NET/IB : Using rocep1s0f1\nAvg bus bandwidth : 0.0")

    assert result.passed is False


def test_runs_head_rdma_client_through_the_head_alias(validate_module):
    """The bound remote method needs the head alias as its first argument."""
    rail = validate_module.Rail("rail100", "enp1s0f1np1", "rocep1s0f1", 3, "192.168.100.10", "192.168.100.11")
    head = validate_module.Host("spark1", "dgx-spark-1", {}, (rail, rail))
    worker = validate_module.Host("spark2", "dgx-spark-2", {}, (rail, rail))

    class Runner:
        def __init__(self):
            self.head = head
            self.calls = []

        def remote(self, host, command, *, check=True):
            self.calls.append(("remote", host, command, check))
            if "nohup" in command:
                return SimpleNamespace(stdout="1234\n", stderr="", returncode=0)
            return SimpleNamespace(
                stdout="Transport type : IB\nLink type : Ethernet\n65536 5000 0.0 88.5 0.1\n",
                stderr="",
                returncode=0,
            )

        def worker_via_fabric(self, command, *, check=True):
            self.calls.append(("worker", command, check))
            return SimpleNamespace(
                stdout="1234\n" if "nohup" in command else "Transport type : IB\nLink type : Ethernet\n65536 5000 0.0 88.5 0.1\n",
                stderr="",
                returncode=0,
            )

    result = validate_module.run_one_rdma(Runner(), worker, head, rail, rail, "ib_write_bw", 12000)

    assert result["passed"] is True
    assert result["client_exit_code"] == 0
    assert result["server_exit_code"] == 0


def test_rejects_nonzero_rdma_server_even_with_positive_output(validate_module):
    """The client metric cannot hide a failed perftest server process."""
    rail = validate_module.Rail("rail100", "enp1s0f1np1", "rocep1s0f1", 3, "192.168.100.10", "192.168.100.11")
    head = validate_module.Host("spark1", "dgx-spark-1", {}, (rail, rail))
    worker = validate_module.Host("spark2", "dgx-spark-2", {}, (rail, rail))
    positive = "Transport type : IB\nLink type : Ethernet\n65536 5000 0.0 88.5 0.1\n"

    class Runner:
        def __init__(self):
            self.head = head

        def remote(self, host, command, *, check=True):
            if "nohup" in command:
                return SimpleNamespace(stdout="1234\n", stderr="", returncode=0)
            return SimpleNamespace(stdout=positive, stderr="server failure", returncode=1)

        def worker_via_fabric(self, command, *, check=True):
            return SimpleNamespace(stdout=positive, stderr="", returncode=0)

    with pytest.raises(validate_module.GateError, match="server exited 1"):
        validate_module.run_one_rdma(Runner(), head, worker, rail, rail, "ib_write_bw", 12000)


def test_native_nccl_prerequisites_require_the_pinned_completed_build(validate_module):
    """The validator verifies the documented host-native result without staging it."""
    command = validate_module.nccl_prerequisite_command()

    assert "https://github.com/NVIDIA/nccl.git" in command
    assert "73cf112295c33aee2b895f329f592f2a9b4b0f97" in command
    assert "a0b82b2260cf5152b9f8c061bbf7eaf0ba096432" in command
    assert "/usr/local/cuda/bin/nvcc" in command
    assert "libnccl.so" in command
    assert "all_reduce_perf" in command
    assert "docker" not in command
    assert "sudo" not in command


def test_native_nccl_launch_uses_restricted_fabric_transport(validate_module):
    """MPI launch cannot weaken the dedicated Spark1-to-Spark2 SSH boundary."""
    head_rail = validate_module.Rail("rail100", "enp1s0f1np1", "rocep1s0f1", 3, "192.168.100.10", "192.168.100.11")
    worker_rail = validate_module.Rail("rail100", "enp1s0f1np1", "rocep1s0f1", 3, "192.168.100.11", "192.168.100.10")
    fabric = {
        "NCCL_SOCKET_IFNAME": "=enp1s0f1np1,enP2p1s0f1np1",
        "NCCL_IB_HCA": "=rocep1s0f1:1,roceP2p1s0f1:1",
        "NCCL_IB_GID_INDEX": 3,
        "TP_SOCKET_IFNAME": "enp1s0f1np1,enP2p1s0f1np1",
        "GLOO_SOCKET_IFNAME": "enp1s0f1np1,enP2p1s0f1np1",
    }
    head = validate_module.Host("spark1", "dgx-spark-1", fabric, (head_rail, head_rail))
    worker = validate_module.Host("spark2", "dgx-spark-2", fabric, (worker_rail, worker_rail))

    command = validate_module.nccl_launch_command(head, worker)

    assert "mpirun -np 2 -H localhost:1,dgx-spark-2-fabric:1" in command
    assert "$HOME/nccl-tests/build/all_reduce_perf" in command
    assert "NCCL_DEBUG='INFO'" in command
    assert "NCCL_SOCKET_IFNAME='=enp1s0f1np1,enP2p1s0f1np1'" in command
    assert "NCCL_IB_HCA='=rocep1s0f1:1,roceP2p1s0f1:1'" in command
    assert "StrictHostKeyChecking=yes" in command
    assert "ForwardAgent=no" in command
    assert "NET/Socket" not in command
    assert "StrictHostKeyChecking=no" not in command
    assert "192.168.1.211" not in command
    assert "192.168.1.212" not in command


def test_native_prerequisite_checks_each_openmpi_package(validate_module):
    """The required OpenMPI packages are verified independently."""
    command = validate_module.nccl_prerequisite_command()

    assert "libopenmpi-dev)" in command
    assert "openmpi-bin)" in command


def test_worker_preflight_precedes_head_preflight(validate_module):
    """Every remote prerequisite gate starts with Spark 2 via the fabric alias."""
    rail = validate_module.Rail("rail100", "enp1s0f1np1", "rocep1s0f1", 3, "192.168.100.10", "192.168.100.11")
    head = validate_module.Host("spark1", "dgx-spark-1", {}, (rail, rail))
    worker = validate_module.Host("spark2", "dgx-spark-2", {}, (rail, rail))

    class Runner:
        def __init__(self):
            self.calls = []

        def remote(self, host, command, *, check=True):
            self.calls.append("head")

        def worker_via_fabric(self, command, *, check=True):
            self.calls.append("worker")

    runner = Runner()
    validate_module.run_preflights(runner, head, worker)

    assert runner.calls == ["worker", "head"]


def test_nccl_validation_is_worker_first_and_launch_only(validate_module):
    """Completed native artifacts are checked, not rebuilt, before the collective."""
    rail = validate_module.Rail("rail100", "enp1s0f1np1", "rocep1s0f1", 3, "192.168.100.10", "192.168.100.11")
    fabric = {"NCCL_SOCKET_IFNAME": "=enp1s0f1np1", "NCCL_IB_HCA": "=rocep1s0f1:1", "NCCL_IB_GID_INDEX": 3, "TP_SOCKET_IFNAME": "enp1s0f1np1", "GLOO_SOCKET_IFNAME": "enp1s0f1np1"}
    head = validate_module.Host("spark1", "dgx-spark-1", fabric, (rail, rail))
    worker = validate_module.Host("spark2", "dgx-spark-2", fabric, (rail, rail))

    class Runner:
        def __init__(self):
            self.calls = []

        def remote(self, host, command, *, check=True):
            self.calls.append(("head", command))
            return SimpleNamespace(stdout="NET/IB : Using rocep1s0f1\nAvg bus bandwidth : 19.3", stderr="", returncode=0)

        def worker_via_fabric(self, command, *, check=True):
            self.calls.append(("worker", command))
            return SimpleNamespace(stdout="", stderr="", returncode=0)

    runner = Runner()
    validate_module.run_nccl(runner, head, worker)

    assert [host for host, _ in runner.calls] == ["worker", "head", "head"]
    assert all("ensure_checkout" not in command for _, command in runner.calls)
