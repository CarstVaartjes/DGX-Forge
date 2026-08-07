import json
import os
import subprocess
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[2]
COLLECTOR = ROOT / "nodes" / "bin" / "collect-inventory"
SCHEMA_PATH = ROOT / "inventory" / "schema.json"


@pytest.fixture
def run_inventory():
    assert COLLECTOR.is_file(), "nodes/bin/collect-inventory does not exist"

    def run():
        completed = subprocess.run(
            ["bash", str(COLLECTOR)],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout)

    return run


def test_inventory_has_required_sections(run_inventory):
    """Catches a collector that omits a required inventory section."""
    result = run_inventory()
    assert set(result) >= {
        "hostname",
        "boot_id",
        "os",
        "kernel",
        "memory",
        "swap",
        "disks",
        "earlyoom",
        "nvidia",
        "docker",
        "interfaces",
        "rdma",
        "thermal",
    }
    assert isinstance(result["interfaces"], list)
    assert isinstance(result["disks"], list)


def test_inventory_satisfies_schema(run_inventory):
    """Catches inventory output that no longer matches the consumer contract."""
    result = run_inventory()
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.validate(result, schema)


def test_inventory_rejects_arguments():
    """Catches a collector that silently accepts unsupported CLI arguments."""
    completed = subprocess.run(
        ["bash", str(COLLECTOR), "unexpected"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""


def test_inventory_tolerates_unparseable_nvidia_smi(tmp_path):
    """Catches optional GPU telemetry that aborts the whole inventory."""
    nvidia_smi = tmp_path / "nvidia-smi"
    nvidia_smi.write_text("#!/usr/bin/env bash\nprintf '0, Test GPU, test-driver, N/A, N/A\\n'\n")
    nvidia_smi.chmod(0o755)

    completed = subprocess.run(
        ["bash", str(COLLECTOR)],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
    )
    result = json.loads(completed.stdout)

    assert result["nvidia"] is None
    assert result["thermal"] is None


def test_captured_inventories_satisfy_schema(inventory_dir):
    """Validates every requested read-only capture against the raw contract."""
    if inventory_dir is None:
        pytest.skip("--inventory-dir was not provided")

    paths = sorted(inventory_dir.glob("*.json"))
    names = {path.name for path in paths}
    assert {"node1-pre.json", "node2-pre.json"} <= names

    schema = json.loads(SCHEMA_PATH.read_text())
    for path in paths:
        with path.open() as inventory_file:
            jsonschema.validate(json.load(inventory_file), schema)
