#!/usr/bin/env python3
"""Fail-closed acceptance checks for the direct two-rail DGX Spark fabric.

The script deliberately uses the head's ``dgx-spark-2-fabric`` SSH alias for
every Spark1-to-Spark2 action.  It never enables agent forwarding and never
copies a private key.  Docker is checked with ``sudo -n`` before any temporary
NCCL staging is attempted, so ordinary callers receive a safe, actionable
failure instead of a password prompt or an implicit docker-group change.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import shlex
import subprocess
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


NCCL_TESTS_COMMIT = "a0b82b2260cf5152b9f8c061bbf7eaf0ba096432"
PYTORCH_IMAGE = "nvcr.io/nvidia/pytorch:25.12-py3"
FABRIC_WORKER_ALIAS = "dgx-spark-2-fabric"
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ForwardAgent=no", "-o", "ConnectTimeout=10")


class GateError(RuntimeError):
    """A failed acceptance gate; no later live gate may run."""


@dataclasses.dataclass(frozen=True)
class NCCLResult:
    passed: bool
    transport: str | None
    bus_bandwidth_gbps: float | None
    reason: str | None = None


@dataclasses.dataclass(frozen=True)
class RDMAResult:
    passed: bool
    bandwidth_gbps: float | None
    reason: str | None = None


@dataclasses.dataclass(frozen=True)
class Rail:
    name: str
    interface: str
    hca: str
    gid_index: int
    fabric_ip: str
    peer_ip: str


@dataclasses.dataclass(frozen=True)
class Host:
    name: str
    ssh_alias: str
    fabric: dict[str, Any]
    rails: tuple[Rail, ...]


def parse_nccl(output: str) -> NCCLResult:
    """Parse NCCL diagnostics and reject socket fallback or absent bandwidth."""
    socket_selected = bool(re.search(r"NET/Socket\b", output, re.IGNORECASE))
    ib_selected = bool(re.search(r"NET/IB\b", output, re.IGNORECASE))
    transport = "Socket" if socket_selected else "IB" if ib_selected else None

    values = [
        float(match)
        for match in re.findall(r"(?:Avg\s+)?bus\s+bandwidth\s*:\s*([0-9]+(?:\.[0-9]+)?)", output, re.I)
    ]
    # Standard nccl-tests rows contain: bytes, iterations, type, op, time,
    # algbw, busbw, error, ... .  The first busbw is enough for acceptance.
    row = re.compile(
        r"^\s*\d+\s+\d+\s+\S+\s+\S+\s+[-+0-9.eE]+\s+[-+0-9.eE]+\s+([-+0-9.eE]+)\b",
        re.MULTILINE,
    )
    values.extend(float(match) for match in row.findall(output))
    bandwidth = max(values) if values else None

    if socket_selected:
        return NCCLResult(False, transport, bandwidth, "NCCL selected NET/Socket")
    if not ib_selected:
        return NCCLResult(False, transport, bandwidth, "NCCL did not report NET/IB")
    if bandwidth is None or bandwidth <= 0:
        return NCCLResult(False, transport, bandwidth, "NCCL reported no positive bus bandwidth")
    return NCCLResult(True, transport, bandwidth)


def parse_rdma(output: str) -> RDMAResult:
    """Require an IB/RoCE perftest with a positive average Gb/s measurement."""
    if not re.search(r"Transport type\s*:\s*IB\b", output, re.I):
        return RDMAResult(False, None, "perftest did not report IB transport")
    if not re.search(r"Link type\s*:\s*Ethernet\b", output, re.I):
        return RDMAResult(False, None, "perftest did not report Ethernet/RoCE link")
    rows = re.compile(r"^\s*\d+\s+\d+\s+[-+0-9.eE]+\s+([-+0-9.eE]+)\s+[-+0-9.eE]+\s*$", re.MULTILINE)
    values = [float(value) for value in rows.findall(output)]
    bandwidth = max(values) if values else None
    if bandwidth is None or bandwidth <= 0:
        return RDMAResult(False, bandwidth, "perftest reported no positive average bandwidth")
    return RDMAResult(True, bandwidth)


def load_hosts(inventory_path: Path) -> tuple[Host, Host]:
    with inventory_path.open("rb") as handle:
        inventory = tomllib.load(handle)
    hosts: list[Host] = []
    for name in ("spark1", "spark2"):
        raw = inventory.get("hosts", {}).get(name)
        if not isinstance(raw, dict):
            raise GateError(f"inventory has no hosts.{name}")
        fabric = raw.get("fabric")
        if not isinstance(fabric, dict):
            raise GateError(f"inventory has no hosts.{name}.fabric")
        rails: list[Rail] = []
        for rail_name, rail in sorted((key, value) for key, value in fabric.items() if key.startswith("rail")):
            if not isinstance(rail, dict):
                raise GateError(f"inventory hosts.{name}.fabric.{rail_name} is not a table")
            required = ("interface", "hca", "gid_index", "fabric_ip", "peer_ip")
            missing = [key for key in required if key not in rail]
            if missing:
                raise GateError(f"inventory hosts.{name}.fabric.{rail_name} missing {', '.join(missing)}")
            rails.append(Rail(rail_name, **{key: rail[key] for key in required}))
        if len(rails) != 2:
            raise GateError(f"inventory hosts.{name} must describe exactly two fabric rails")
        hosts.append(Host(name, raw["ssh_alias"], fabric, tuple(rails)))
    return hosts[0], hosts[1]


def validate_consumers(head: Host, worker: Host) -> None:
    """Reject a stale inventory before it can select different HCAs/GIDs."""
    if [rail.name for rail in head.rails] != [rail.name for rail in worker.rails]:
        raise GateError("Spark rail names do not match")
    for left, right in zip(head.rails, worker.rails, strict=True):
        if (left.interface, left.hca, left.gid_index) != (right.interface, right.hca, right.gid_index):
            raise GateError(f"mismatched HCA/GID consumers on {left.name}")
        if left.peer_ip != right.fabric_ip or right.peer_ip != left.fabric_ip:
            raise GateError(f"mismatched fabric peer IPs on {left.name}")
    interfaces = ",".join(rail.interface for rail in head.rails)
    hcas = ",".join(f"{rail.hca}:1" for rail in head.rails)
    expected = {
        "NCCL_SOCKET_IFNAME": f"={interfaces}",
        "NCCL_IB_HCA": f"={hcas}",
        "NCCL_IB_GID_INDEX": head.rails[0].gid_index,
        "TP_SOCKET_IFNAME": interfaces,
        "GLOO_SOCKET_IFNAME": interfaces,
    }
    for host in (head, worker):
        for variable, value in expected.items():
            if host.fabric.get(variable) != value:
                raise GateError(f"{host.name} {variable} does not match the two recorded rails")
        if any(rail.gid_index != expected["NCCL_IB_GID_INDEX"] for rail in host.rails):
            raise GateError(f"{host.name} uses different GID indices across rails")


def command_record(command: list[str], completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "command": shlex.join(command),
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


class Runner:
    def __init__(self, head: Host, worker: Host, evidence: list[dict[str, Any]]):
        self.head = head
        self.worker = worker
        self.evidence = evidence

    def local(
        self, command: list[str], *, check: bool = True, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(command, input=input_text, capture_output=True, text=True, check=False)
        self.evidence.append(command_record(command, completed))
        if check and completed.returncode:
            raise GateError(f"command failed ({completed.returncode}): {shlex.join(command)}")
        return completed

    def remote(self, host: str, shell_command: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        # Supplying the body on stdin avoids OpenSSH's lossy joining of remote
        # argv and makes multi-line safety checks unambiguous.
        return self.local(["ssh", *SSH_OPTIONS, host, "bash", "-s"], check=check, input_text=shell_command)

    def worker_via_fabric(self, shell_command: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        nested = "printf %s " + shlex.quote(shell_command) + " | ssh "
        nested += " ".join(shlex.quote(item) for item in (*SSH_OPTIONS, FABRIC_WORKER_ALIAS))
        nested += " bash -s"
        return self.remote(self.head.ssh_alias, nested, check=check)


def remote_preflight(runner: Runner, host: Host, *, via_fabric: bool) -> None:
    command = """
set -euo pipefail
test -x /usr/bin/ib_write_bw
test -x /usr/bin/ib_read_bw
test -x /usr/bin/ibv_devinfo
command -v rdma >/dev/null
"""
    for rail in host.rails:
        command += (
            f"test -r /sys/class/net/{shlex.quote(rail.interface)}/mtu\n"
            f"test -r /sys/class/infiniband/{shlex.quote(rail.hca)}/ports/1/gids/{rail.gid_index}\n"
            f"test -r /sys/class/infiniband/{shlex.quote(rail.hca)}/ports/1/gid_attrs/ndevs/{rail.gid_index}\n"
            f"test \"$(cat /sys/class/infiniband/{shlex.quote(rail.hca)}/ports/1/gid_attrs/ndevs/{rail.gid_index})\" = {shlex.quote(rail.interface)}\n"
            f"test -z \"$(ip route show default dev {shlex.quote(rail.interface)})\"\n"
        )
    if via_fabric:
        runner.worker_via_fabric(command)
    else:
        runner.remote(host.ssh_alias, command)


def perftest_command(tool: str, rail: Rail, peer_ip: str, port: int, *, server: bool) -> str:
    base = (
        f"/usr/bin/{tool} -d {shlex.quote(rail.hca)} -i 1 -x {rail.gid_index} -p {port} "
        "-F --report_gbits --size 65536 --iters 5000"
    )
    return base if server else f"{base} {shlex.quote(peer_ip)}"


def run_one_rdma(
    runner: Runner,
    server_host: Host,
    client_host: Host,
    server_rail: Rail,
    client_rail: Rail,
    tool: str,
    port: int,
) -> dict[str, Any]:
    label = f"{tool}:{client_host.name}->{server_host.name}:{server_rail.name}"
    server_log = f"/tmp/validate-fabric-{tool}-{port}.log"
    server_command = (
        f"rm -f {server_log}; nohup {perftest_command(tool, server_rail, '', port, server=True)} "
        f">{server_log} 2>&1 & echo $!"
    )
    def call_on(host: Host, command: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        if host is runner.head:
            return runner.remote(host.ssh_alias, command, check=check)
        return runner.worker_via_fabric(command, check=check)

    server_call = lambda command, check=True: call_on(server_host, command, check=check)
    client_call = lambda command, check=True: call_on(client_host, command, check=check)
    server_pid = server_call(server_command).stdout.strip()
    if not server_pid.isdigit():
        raise GateError(f"{label} did not return a perftest server PID")
    try:
        time.sleep(1)
        client = client_call(perftest_command(tool, client_rail, server_rail.fabric_ip, port, server=False))
        server_log_output = server_call(f"kill {server_pid} 2>/dev/null || true; cat {server_log}; rm -f {server_log}", check=False)
    except BaseException:
        server_call(f"kill {server_pid} 2>/dev/null || true; rm -f {server_log}", check=False)
        raise
    parsed = parse_rdma(client.stdout + "\n" + server_log_output.stdout)
    if not parsed.passed:
        raise GateError(f"{label}: {parsed.reason}")
    return {"name": label, "passed": True, "bandwidth_gbps": parsed.bandwidth_gbps}


def run_rdma(runner: Runner, head: Host, worker: Host) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    port = 12000
    for tool in ("ib_write_bw", "ib_read_bw"):
        for head_rail, worker_rail in zip(head.rails, worker.rails, strict=True):
            results.append(run_one_rdma(runner, worker, head, worker_rail, head_rail, tool, port))
            port += 1
            results.append(run_one_rdma(runner, head, worker, head_rail, worker_rail, tool, port))
            port += 1
    return results


def run_nccl(runner: Runner, head: Host, worker: Host) -> NCCLResult:
    # This gate intentionally comes before any temporary source/image staging.
    # A failing sudo -n proves that a human must provide the narrow sudo action.
    runner.remote(head.ssh_alias, "sudo -n /usr/bin/docker info >/dev/null")
    runner.worker_via_fabric("sudo -n /usr/bin/docker info >/dev/null")
    raise GateError(
        "NCCL temporary-container runner is intentionally staged only after explicit sudo authorization; "
        "see docs/runbooks/fabric.md"
    )


def write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true", help="run only non-mutating inventory and host checks")
    args = parser.parse_args(argv)

    evidence: list[dict[str, Any]] = []
    document: dict[str, Any] = {
        "schema_version": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "evidence_scope": "live_runtime_verification",
        "status": "failed",
        "rdma": [],
        "nccl": None,
        "commands": evidence,
    }
    try:
        head, worker = load_hosts(args.inventory)
        validate_consumers(head, worker)
        document["inventory"] = str(args.inventory)
        document["resolved_consumers"] = {
            key: head.fabric[key]
            for key in ("NCCL_SOCKET_IFNAME", "NCCL_IB_HCA", "NCCL_IB_GID_INDEX", "TP_SOCKET_IFNAME", "GLOO_SOCKET_IFNAME")
        }
        runner = Runner(head, worker, evidence)
        remote_preflight(runner, head, via_fabric=False)
        remote_preflight(runner, worker, via_fabric=True)
        if args.preflight_only:
            document["status"] = "preflight_passed"
            document["evidence_scope"] = "live_read_only_preflight"
            return 0
        document["rdma"] = run_rdma(runner, head, worker)
        nccl = run_nccl(runner, head, worker)
        document["nccl"] = dataclasses.asdict(nccl)
        if not nccl.passed:
            raise GateError(nccl.reason or "NCCL failed")
        document["status"] = "passed"
        return 0
    except GateError as error:
        document["failure"] = str(error)
        return 1
    finally:
        write_json(args.output, document)


if __name__ == "__main__":
    sys.exit(main())
