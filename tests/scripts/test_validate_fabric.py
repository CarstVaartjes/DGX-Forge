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
                return SimpleNamespace(stdout="1234\n")
            return SimpleNamespace(stdout="Transport type : IB\nLink type : Ethernet\n65536 5000 0.0 88.5 0.1\n")

        def worker_via_fabric(self, command, *, check=True):
            self.calls.append(("worker", command, check))
            return SimpleNamespace(stdout="1234\n" if "nohup" in command else "")

    result = validate_module.run_one_rdma(Runner(), worker, head, rail, rail, "ib_write_bw", 12000)

    assert result["passed"] is True
