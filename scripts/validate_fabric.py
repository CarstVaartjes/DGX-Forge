#!/usr/bin/env python3
"""Fail-closed acceptance checks for the direct two-rail DGX Spark fabric.

The script deliberately uses the head's ``dgx-spark-2-fabric`` SSH alias for
every Spark1-to-Spark2 action.  It never enables agent forwarding and never
copies a private key.  NCCL is built natively from pinned NVIDIA sources only
as a documented prerequisite; this validator verifies the completed artifacts
worker-first before the live fabric gates.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
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


NCCL_VERSION = "v2.30.7-1"
NCCL_COMMIT = "73cf112295c33aee2b895f329f592f2a9b4b0f97"
NCCL_TESTS_COMMIT = "a0b82b2260cf5152b9f8c061bbf7eaf0ba096432"
CUDA_NVCC = "/usr/local/cuda/bin/nvcc"
MPI_HOME = "/usr/lib/aarch64-linux-gnu/openmpi"
FABRIC_WORKER_ALIAS = "dgx-spark-2-fabric"
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ForwardAgent=no", "-o", "ConnectTimeout=10")
PHYSICAL_LINK_MIN_GBPS = 184.0
WRITE_FUNCTION_MIN_GBPS = 98.01
READ_FUNCTION_MIN_GBPS = 72.37
NCCL_MIN_GB_PER_SECOND = 17.44


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
    ib_selected = bool(re.search(r"NET/IB\s*:\s*Using\b", output, re.IGNORECASE))
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
        return NCCLResult(False, transport, bandwidth, "NCCL did not report NET/IB : Using")
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


def perftest_command(
    tool: str,
    rail: Rail,
    peer_ip: str,
    port: int,
    *,
    server: bool,
    duration_seconds: int | None = None,
) -> str:
    run_length = f"--duration {duration_seconds}" if duration_seconds is not None else "--iters 5000"
    base = (
        f"/usr/bin/{tool} -d {shlex.quote(rail.hca)} -i 1 -x {rail.gid_index} -p {port} "
        f"-F --report_gbits --size 65536 {run_length}"
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
    *,
    minimum_bandwidth_gbps: float = 0.0,
    duration_seconds: int | None = None,
) -> dict[str, Any]:
    label = f"{tool}:{client_host.name}->{server_host.name}:{server_rail.name}"
    server_log = f"/tmp/validate-fabric-{tool}-{port}.log"
    server_status = f"/tmp/validate-fabric-{tool}-{port}.status"
    server_body = (
        f'{perftest_command(tool, server_rail, "", port, server=True, duration_seconds=duration_seconds)} > "$1" 2>&1; '
        'exit_code=$?; printf "%s\\n" "$exit_code" > "$2"; exit "$exit_code"'
    )
    server_command = (
        f"rm -f {server_log} {server_status}; nohup bash -c {shlex.quote(server_body)} "
        f"validate-fabric-perftest {shlex.quote(server_log)} {shlex.quote(server_status)} "
        "</dev/null >/dev/null 2>&1 & echo $!"
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
    collect_server = f"""set -u
for _ in $(seq 1 300); do
  [ -s {shlex.quote(server_status)} ] && break
  kill -0 {server_pid} 2>/dev/null || break
  sleep 0.1
done
if [ ! -s {shlex.quote(server_status)} ]; then
  kill {server_pid} 2>/dev/null || true
  cat {shlex.quote(server_log)} 2>/dev/null || true
  rm -f {shlex.quote(server_log)} {shlex.quote(server_status)}
  exit 124
fi
exit_code="$(cat {shlex.quote(server_status)})"
cat {shlex.quote(server_log)}
rm -f {shlex.quote(server_log)} {shlex.quote(server_status)}
case "$exit_code" in
  ''|*[!0-9]*) exit 125 ;;
esac
exit "$exit_code"
    """
    try:
        time.sleep(1)
        started_monotonic = time.monotonic()
        client = client_call(
            perftest_command(
                tool,
                client_rail,
                server_rail.fabric_ip,
                port,
                server=False,
                duration_seconds=duration_seconds,
            ),
            check=False,
        )
        finished_monotonic = time.monotonic()
        server = server_call(collect_server, check=False)
    except BaseException:
        server_call(f"kill {server_pid} 2>/dev/null || true; rm -f {server_log} {server_status}", check=False)
        raise
    if client.returncode:
        raise GateError(f"{label} client exited {client.returncode}")
    if server.returncode:
        raise GateError(f"{label} server exited {server.returncode}")
    parsed = parse_rdma(client.stdout + "\n" + client.stderr + "\n" + server.stdout + "\n" + server.stderr)
    if not parsed.passed:
        raise GateError(f"{label}: {parsed.reason}")
    if parsed.bandwidth_gbps is None or parsed.bandwidth_gbps < minimum_bandwidth_gbps:
        raise GateError(
            f"{label} bandwidth {parsed.bandwidth_gbps or 0.0:.2f} Gb/s "
            f"is below {minimum_bandwidth_gbps:.2f} Gb/s"
        )
    return {
        "name": label,
        "passed": True,
        "bandwidth_gbps": parsed.bandwidth_gbps,
        "minimum_bandwidth_gbps": minimum_bandwidth_gbps,
        "started_monotonic": started_monotonic,
        "finished_monotonic": finished_monotonic,
        "client_exit_code": client.returncode,
        "server_exit_code": server.returncode,
    }


def run_aggregate_rdma_write(
    runner: Runner,
    server_host: Host,
    client_host: Host,
    *,
    base_port: int,
    run_component=run_one_rdma,
) -> dict[str, Any]:
    """Run both RoCE functions concurrently and enforce physical-link bandwidth."""
    pairs = list(zip(server_host.rails, client_host.rails, strict=True))
    if len(pairs) != 2:
        raise GateError("aggregate RDMA requires exactly two RoCE functions")
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                run_component,
                runner,
                server_host,
                client_host,
                server_rail,
                client_rail,
                "ib_write_bw",
                base_port + index,
                minimum_bandwidth_gbps=0.0,
                duration_seconds=5,
            )
            for index, (server_rail, client_rail) in enumerate(pairs)
        ]
        components = [future.result() for future in futures]
    if len(components) != 2 or any(not component.get("passed") for component in components):
        raise GateError("aggregate RDMA requires two successful component results")
    overlap_seconds = min(component["finished_monotonic"] for component in components) - max(
        component["started_monotonic"] for component in components
    )
    if overlap_seconds <= 0:
        raise GateError("aggregate RDMA component intervals did not overlap")
    aggregate = sum(float(component["bandwidth_gbps"]) for component in components)
    if aggregate < PHYSICAL_LINK_MIN_GBPS:
        raise GateError(
            f"aggregate RDMA write {client_host.name}->{server_host.name} "
            f"{aggregate:.2f} Gb/s is below {PHYSICAL_LINK_MIN_GBPS:.2f} Gb/s"
        )
    return {
        "name": f"ib_write_bw:aggregate:{client_host.name}->{server_host.name}",
        "passed": True,
        "aggregate_bandwidth_gbps": aggregate,
        "minimum_bandwidth_gbps": PHYSICAL_LINK_MIN_GBPS,
        "overlap_seconds": overlap_seconds,
        "components": components,
    }


def run_rdma(runner: Runner, head: Host, worker: Host) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    port = 12000
    for tool in ("ib_write_bw", "ib_read_bw"):
        minimum = WRITE_FUNCTION_MIN_GBPS if tool == "ib_write_bw" else READ_FUNCTION_MIN_GBPS
        for head_rail, worker_rail in zip(head.rails, worker.rails, strict=True):
            results.append(
                run_one_rdma(
                    runner,
                    worker,
                    head,
                    worker_rail,
                    head_rail,
                    tool,
                    port,
                    minimum_bandwidth_gbps=minimum,
                )
            )
            port += 1
            results.append(
                run_one_rdma(
                    runner,
                    head,
                    worker,
                    head_rail,
                    worker_rail,
                    tool,
                    port,
                    minimum_bandwidth_gbps=minimum,
                )
            )
            port += 1
    results.append(run_aggregate_rdma_write(runner, worker, head, base_port=13000))
    results.append(run_aggregate_rdma_write(runner, head, worker, base_port=13100))
    return results


def nccl_prerequisite_command() -> str:
    """Read-only checks for the documented completed native NCCL build."""
    return f"""set -euo pipefail
check_completed_checkout() {{
  directory="$1" repository="$2" revision="$3"
  test -d "$directory"
  test ! -L "$directory"
  test -d "$directory/.git"
  test ! -L "$directory/.git"
  test "$(git -C "$directory" remote get-url origin)" = "$repository"
  test "$(git -C "$directory" rev-parse HEAD)" = "$revision"
}}
test -x {CUDA_NVCC}
{CUDA_NVCC} --version
command -v git >/dev/null
command -v mpirun >/dev/null
test "$(dpkg-query -W -f='${{db:Status-Status}} ${{Version}}' libopenmpi-dev)" = "installed 4.1.6-7ubuntu2"
test "$(dpkg-query -W -f='${{db:Status-Status}} ${{Version}}' openmpi-bin)" = "installed 4.1.6-7ubuntu2"
check_completed_checkout "$HOME/nccl" https://github.com/NVIDIA/nccl.git {NCCL_COMMIT}
check_completed_checkout "$HOME/nccl-tests" https://github.com/NVIDIA/nccl-tests.git {NCCL_TESTS_COMMIT}
test -r "$HOME/nccl/build/lib/libnccl.so"
test -x "$HOME/nccl-tests/build/all_reduce_perf"
"""


def nccl_launch_command(head: Host, worker: Host) -> str:
    """Launch a two-rank all-reduce only through the restricted fabric alias."""
    fabric = head.fabric
    exports = {
        "NCCL_DEBUG": "INFO",
        "NCCL_SOCKET_IFNAME": fabric["NCCL_SOCKET_IFNAME"],
        "NCCL_IB_HCA": fabric["NCCL_IB_HCA"],
        "NCCL_IB_GID_INDEX": str(fabric["NCCL_IB_GID_INDEX"]),
        "TP_SOCKET_IFNAME": fabric["TP_SOCKET_IFNAME"],
        "GLOO_SOCKET_IFNAME": fabric["GLOO_SOCKET_IFNAME"],
        "OMPI_MCA_oob_tcp_if_include": fabric["TP_SOCKET_IFNAME"],
        "OMPI_MCA_btl_tcp_if_include": fabric["TP_SOCKET_IFNAME"],
    }
    export_lines = "\n".join(f"export {key}='{value}'" for key, value in exports.items())
    x_args = " ".join(f"-x {key}" for key in exports)
    return f"""set -euo pipefail
export CUDA_HOME=/usr/local/cuda
export MPI_HOME={MPI_HOME}
export NCCL_HOME="$HOME/nccl/build"
export LD_LIBRARY_PATH="$NCCL_HOME/lib:$CUDA_HOME/lib64:$MPI_HOME/lib:${{LD_LIBRARY_PATH:-}}"
{export_lines}
test -x "$HOME/nccl-tests/build/all_reduce_perf"
mpirun -np 2 -H localhost:1,{FABRIC_WORKER_ALIAS}:1 \\
  --mca plm_rsh_agent "ssh -o BatchMode=yes -o ForwardAgent=no -o StrictHostKeyChecking=yes" \\
  {x_args} -x LD_LIBRARY_PATH \\
  "$HOME/nccl-tests/build/all_reduce_perf" -b 8M -e 1G -f 2 -g 1 -c 1
"""


def run_nccl(runner: Runner, head: Host, worker: Host) -> NCCLResult:
    # The worker prerequisite is deliberately first. No source staging, sudo,
    # agent forwarding, management-plane host list, or shared key is involved.
    runner.worker_via_fabric(nccl_prerequisite_command())
    runner.remote(head.ssh_alias, nccl_prerequisite_command())
    result = runner.remote(head.ssh_alias, nccl_launch_command(head, worker))
    parsed = parse_nccl(result.stdout + "\n" + result.stderr)
    if not parsed.passed:
        raise GateError(parsed.reason or "NCCL all-reduce failed")
    if parsed.bus_bandwidth_gbps is None or parsed.bus_bandwidth_gbps < NCCL_MIN_GB_PER_SECOND:
        raise GateError(
            f"NCCL bus bandwidth {parsed.bus_bandwidth_gbps or 0.0:.2f} GB/s "
            f"is below {NCCL_MIN_GB_PER_SECOND:.2f} GB/s"
        )
    return parsed


def write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def run_preflights(runner: Runner, head: Host, worker: Host) -> None:
    """Check the worker first at every remote prerequisite boundary."""
    remote_preflight(runner, worker, via_fabric=True)
    remote_preflight(runner, head, via_fabric=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true", help="run only non-mutating inventory and host checks")
    parser.add_argument(
        "--nccl-preflight-only",
        action="store_true",
        help="also verify native NCCL/MPI prerequisites on worker then head without staging sources",
    )
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
        run_preflights(runner, head, worker)
        if args.preflight_only:
            document["status"] = "preflight_passed"
            document["evidence_scope"] = "live_read_only_preflight"
            return 0
        if args.nccl_preflight_only:
            runner.worker_via_fabric(nccl_prerequisite_command())
            runner.remote(head.ssh_alias, nccl_prerequisite_command())
            document["status"] = "nccl_preflight_passed"
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
