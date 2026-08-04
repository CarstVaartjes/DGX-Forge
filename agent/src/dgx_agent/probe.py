"""Bounded, read-only node probing through an installed fixed policy."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import ipaddress
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
import re
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

from dgx_agent_protocol import canonical_message

from .nvidia_tools import (
    InstalledPolicy,
    InstalledPolicyError,
    ToolName,
    open_verified_executable,
    open_verified_support_archive,
    parse_tool_document,
)


TOTAL_PROBE_SECONDS = 15
AGGREGATE_OUTPUT_LIMIT_BYTES = 256 * 1024
RESULT_LIMIT_BYTES = 64 * 1024
_CLEANUP_RESERVE_SECONDS = 0.04
FIXED_PROCESS_ENVIRONMENT: Mapping[str, str] = MappingProxyType(
    {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
)


class ProbeError(RuntimeError):
    error_code = "probe_failed"

    def __init__(self, message: str, *, captured_bytes: int = 0) -> None:
        super().__init__(message)
        self.captured_bytes = max(0, captured_bytes)


class ProbeDeadlineExceeded(ProbeError):
    error_code = "probe_timeout"


class ProbeOutputLimitExceeded(ProbeError):
    error_code = "probe_output_limit"


class ProbeResultLimitExceeded(ProbeError):
    error_code = "probe_result_limit"


class ProbeCollectorError(ProbeError):
    error_code = "probe_collector_failed"


@dataclass(frozen=True)
class ProcessRequest:
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    timeout_seconds: float
    output_limit_bytes: int
    executable_fd: int
    support_archive_fd: int | None = None
    absolute_deadline: float | None = None
    shell: bool = False
    stdin_closed: bool = True
    close_fds: bool = True
    new_process_group: bool = True

    @classmethod
    def fixed(
        cls,
        *,
        argv: tuple[str, ...],
        cwd: Path,
        timeout_seconds: float,
        output_limit_bytes: int,
        executable_fd: int,
        support_archive_fd: int | None = None,
        absolute_deadline: float | None = None,
    ) -> "ProcessRequest":
        if (
            not argv
            or not isinstance(argv[0], str)
            or not Path(argv[0]).is_absolute()
            or any(not isinstance(value, str) or "\x00" in value for value in argv)
        ):
            raise ProbeCollectorError("process request argv is invalid")
        path = Path(cwd)
        if not path.is_absolute():
            raise ProbeCollectorError("process request cwd is invalid")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ProbeDeadlineExceeded("process deadline has elapsed")
        if (
            not isinstance(output_limit_bytes, int)
            or isinstance(output_limit_bytes, bool)
            or output_limit_bytes <= 0
            or output_limit_bytes > AGGREGATE_OUTPUT_LIMIT_BYTES
        ):
            raise ProbeOutputLimitExceeded("process output limit is invalid")
        if not isinstance(executable_fd, int) or executable_fd < 0:
            raise ProbeCollectorError("verified executable descriptor is invalid")
        try:
            os.fstat(executable_fd)
        except OSError as error:
            raise ProbeCollectorError("verified executable descriptor is invalid") from error
        if support_archive_fd is not None:
            try:
                os.fstat(support_archive_fd)
            except OSError as error:
                raise ProbeCollectorError("verified support descriptor is invalid") from error
        now = time.monotonic()
        fixed_deadline = min(
            now + float(timeout_seconds),
            absolute_deadline if absolute_deadline is not None else math.inf,
        )
        if not math.isfinite(fixed_deadline) or fixed_deadline <= now:
            raise ProbeDeadlineExceeded("process deadline has elapsed")
        return cls(
            tuple(argv),
            path,
            FIXED_PROCESS_ENVIRONMENT,
            float(timeout_seconds),
            output_limit_bytes,
            executable_fd,
            support_archive_fd,
            fixed_deadline,
        )

    @property
    def inherited_fds(self) -> tuple[int, ...]:
        if self.support_archive_fd is None:
            return (self.executable_fd,)
        return (self.executable_fd, self.support_archive_fd)


@dataclass(frozen=True)
class ProcessOutcome:
    returncode: int
    stdout: bytes
    stderr: bytes


class _ProcessRunner(Protocol):
    def run(self, request: ProcessRequest) -> ProcessOutcome: ...


class BoundedProcessRunner:
    """Capture stdout/stderr incrementally and kill the process group on bounds."""

    def run(self, request: ProcessRequest) -> ProcessOutcome:
        process: subprocess.Popen[bytes] | None = None
        status_read = -1
        status_write = -1
        deadline = request.absolute_deadline
        if deadline is None:
            deadline = time.monotonic() + request.timeout_seconds
        execution_deadline = deadline - _CLEANUP_RESERVE_SECONDS
        if time.monotonic() >= execution_deadline:
            raise ProbeDeadlineExceeded("probe process timed out")
        try:
            supervisor = Path(__file__).with_name("_probe_supervisor.py")
            status_read, status_write = os.pipe2(os.O_CLOEXEC)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(supervisor),
                    str(status_write),
                    str(request.executable_fd),
                    str(request.support_archive_fd if request.support_archive_fd is not None else -1),
                    repr(deadline),
                    str(request.cwd),
                    *request.argv,
                ],
                executable=sys.executable,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="/",
                env=dict(request.env),
                close_fds=True,
                pass_fds=(*request.inherited_fds, status_write),
                start_new_session=True,
                text=False,
                bufsize=0,
            )
            os.close(status_write)
            status_write = -1
            assert process.stdout is not None and process.stderr is not None
            streams = {
                process.stdout.fileno(): bytearray(),
                process.stderr.fileno(): bytearray(),
            }
            for descriptor in streams:
                os.set_blocking(descriptor, False)
            selector = selectors.DefaultSelector()
            try:
                for descriptor in streams:
                    selector.register(descriptor, selectors.EVENT_READ)
                total = 0
                while selector.get_map():
                    remaining_time = execution_deadline - time.monotonic()
                    if remaining_time <= 0:
                        raise ProbeDeadlineExceeded(
                            "probe process timed out", captured_bytes=total
                        )
                    events = selector.select(min(remaining_time, 0.1))
                    if not events and process.poll() is not None:
                        # A final nonblocking read below observes EOF and drains
                        # bytes already in the pipe.
                        events = [
                            (key, selectors.EVENT_READ)
                            for key in tuple(selector.get_map().values())
                        ]
                    for key, _ in events:
                        allowance = request.output_limit_bytes - total
                        chunk = os.read(key.fd, min(64 * 1024, allowance + 1))
                        if not chunk:
                            selector.unregister(key.fd)
                            continue
                        total += len(chunk)
                        if total > request.output_limit_bytes:
                            raise ProbeOutputLimitExceeded(
                                "probe process output exceeded limit",
                                captured_bytes=total,
                            )
                        streams[key.fd].extend(chunk)
                remaining_time = execution_deadline - time.monotonic()
                if remaining_time <= 0:
                    raise ProbeDeadlineExceeded(
                        "probe process timed out", captured_bytes=total
                    )
                try:
                    returncode = process.wait(timeout=remaining_time)
                except subprocess.TimeoutExpired as error:
                    raise ProbeDeadlineExceeded(
                        "probe process timed out", captured_bytes=total
                    ) from error
                status = os.read(status_read, 33)
                if time.monotonic() >= deadline:
                    raise ProbeDeadlineExceeded(
                        "probe process timed out", captured_bytes=total
                    )
                if returncode != 0 or len(status) > 32 or not status.endswith(b"\n"):
                    raise ProbeCollectorError("probe process could not be executed")
                try:
                    kind, raw_code = status[:-1].decode("ascii").split(":", 1)
                    tool_returncode = int(raw_code)
                except (UnicodeDecodeError, ValueError) as error:
                    raise ProbeCollectorError(
                        "probe supervisor returned invalid status"
                    ) from error
                if kind == "timeout":
                    raise ProbeDeadlineExceeded(
                        "probe process timed out", captured_bytes=total
                    )
                if kind != "ok" or not 0 <= tool_returncode <= 125:
                    raise ProbeCollectorError("probe process could not be executed")
                return ProcessOutcome(
                    tool_returncode,
                    bytes(streams[process.stdout.fileno()]),
                    bytes(streams[process.stderr.fileno()]),
                )
            finally:
                selector.close()
        except (ProbeDeadlineExceeded, ProbeOutputLimitExceeded):
            if process is not None:
                _terminate_group(process, deadline)
            raise
        except (OSError, subprocess.SubprocessError) as error:
            if process is not None:
                _terminate_group(process, deadline)
            raise ProbeCollectorError("probe process could not be executed") from error
        finally:
            if process is not None:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
            if status_read >= 0:
                os.close(status_read)
            if status_write >= 0:
                os.close(status_write)


def _terminate_group(
    process: subprocess.Popen[bytes], absolute_deadline: float
) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        os.kill(process.pid, signal.SIGCONT)
    except ProcessLookupError:
        pass
    cleanup_end = min(time.monotonic() + 0.08, absolute_deadline + 0.08)
    try:
        process.wait(timeout=max(0.0, cleanup_end - time.monotonic()))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=max(0.0, cleanup_end - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=0.01)
            except subprocess.TimeoutExpired:
                pass


@dataclass(frozen=True)
class PinnedNodeProbe:
    policy: InstalledPolicy
    _runner: _ProcessRunner = BoundedProcessRunner()
    _monotonic: Callable[[], float] = time.monotonic
    _utcnow: Callable[[], datetime] = lambda: datetime.now(UTC)

    def collect(self, deadline: datetime) -> Mapping[str, Any]:
        if (
            not isinstance(deadline, datetime)
            or deadline.tzinfo is None
            or deadline.utcoffset() != UTC.utcoffset(deadline)
        ):
            raise ProbeDeadlineExceeded("probe deadline is invalid")
        claim_remaining = (deadline.astimezone(UTC) - self._utcnow()).total_seconds()
        if claim_remaining <= 0:
            raise ProbeDeadlineExceeded("probe deadline has elapsed")
        absolute_deadline = self._monotonic() + min(TOTAL_PROBE_SECONDS, claim_remaining)

        descriptors: dict[ToolName, int | None] = {}
        opened: list[int] = []
        support_archive: int | None = None
        try:
            # Open and hash every path before executing anything.  Retaining
            # these descriptors closes the verify/open pathname race.
            if self.policy.bundle_root_available():
                support_archive = open_verified_support_archive(
                    self.policy,
                    _check_deadline=lambda: self._ensure_time(absolute_deadline),
                )
                opened.append(support_archive)
                self._ensure_time(absolute_deadline)
                for tool in self.policy.tools:
                    descriptor = open_verified_executable(
                        tool.executable,
                        tool.sha256,
                        _test_only_allow_unprivileged=(
                            self.policy._test_only_allow_unprivileged
                        ),
                        _check_deadline=lambda: self._ensure_time(absolute_deadline),
                    )
                    descriptors[tool.name] = descriptor
                    if descriptor is not None:
                        opened.append(descriptor)
                    self._ensure_time(absolute_deadline)
            else:
                descriptors.update({tool.name: None for tool in self.policy.tools})
            health_descriptor = open_verified_executable(
                self.policy.health.executable,
                self.policy.health.sha256,
                _test_only_allow_unprivileged=(
                    self.policy._test_only_allow_unprivileged
                ),
                _check_deadline=lambda: self._ensure_time(absolute_deadline),
            )
            if health_descriptor is None:
                raise ProbeCollectorError("fixed health collector is unavailable")
            opened.append(health_descriptor)
            self._ensure_time(absolute_deadline)

            aggregate = 0
            health_outcome = self._run(
                (str(self.policy.health.executable), *self.policy.health.arguments),
                Path("/"),
                self.policy.health.timeout_seconds,
                self.policy.health.output_limit_bytes,
                absolute_deadline,
                aggregate,
                health_descriptor,
                support_archive_fd=None,
                tool_environment=False,
            )
            aggregate = _add_raw(aggregate, health_outcome)
            if health_outcome.returncode != 0:
                raise ProbeCollectorError("fixed health collector failed")
            health = _normalize_health(health_outcome.stdout, self.policy.health.output_limit_bytes)
            self._ensure_time(absolute_deadline)

            tools: dict[str, Mapping[str, Any]] = {}
            for tool in self.policy.tools:
                provenance = {"version": tool.version, "sha256": tool.sha256}
                descriptor = descriptors[tool.name]
                if descriptor is None:
                    tools[tool.name.value] = {"status": "unavailable", **provenance}
                    continue
                try:
                    outcome = self._run(
                        (str(tool.executable), *tool.arguments),
                        self.policy.bundle_root,
                        tool.timeout_seconds,
                        tool.output_limit_bytes,
                        absolute_deadline,
                        aggregate,
                        descriptor,
                        support_archive_fd=support_archive,
                        tool_environment=True,
                    )
                    aggregate = _add_raw(aggregate, outcome)
                    if outcome.returncode != 0:
                        tools[tool.name.value] = {
                            "status": "degraded", **provenance,
                            "error_code": "tool_nonzero_exit",
                        }
                        continue
                    parsed = parse_tool_document(
                        tool.name, outcome.stdout, limit=tool.output_limit_bytes
                    )
                    self._ensure_time(absolute_deadline)
                    item: dict[str, Any] = {
                        "status": "ok" if parsed.ok else "degraded",
                        **provenance,
                        "data": parsed.data,
                    }
                    if not parsed.ok:
                        item["error_code"] = "tool_reported_failure"
                    tools[tool.name.value] = item
                except ProbeOutputLimitExceeded as error:
                    aggregate = _add_captured(aggregate, error.captured_bytes)
                    tools[tool.name.value] = {
                        "status": "degraded", **provenance,
                        "error_code": "tool_output_limit",
                    }
                except ProbeDeadlineExceeded as error:
                    aggregate = _add_captured(aggregate, error.captured_bytes)
                    if absolute_deadline - self._monotonic() <= 0:
                        raise
                    tools[tool.name.value] = {
                        "status": "degraded", **provenance,
                        "error_code": "tool_timeout",
                    }
                except InstalledPolicyError:
                    tools[tool.name.value] = {
                        "status": "unsupported", **provenance,
                        "error_code": "tool_output_incompatible",
                    }
        finally:
            for descriptor in opened:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

        evidence = {
            "dgx_forge": health,
            "nvidia": {
                "bundle_version": self.policy.bundle_version,
                "bundle_sha256": self.policy.bundle_sha256,
                "tools": tools,
            },
        }
        frozen = _freeze(evidence)
        if len(canonical_message({"status": "ok", "evidence": frozen})) > RESULT_LIMIT_BYTES:
            raise ProbeResultLimitExceeded("normalized probe result is too large")
        self._ensure_time(absolute_deadline)
        return frozen

    def _run(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        policy_timeout: int,
        policy_output_limit: int,
        absolute_deadline: float,
        aggregate: int,
        executable_fd: int,
        *,
        support_archive_fd: int | None,
        tool_environment: bool,
    ) -> ProcessOutcome:
        remaining_time = absolute_deadline - self._monotonic()
        if remaining_time <= 0:
            raise ProbeDeadlineExceeded("probe deadline has elapsed")
        remaining_output = AGGREGATE_OUTPUT_LIMIT_BYTES - aggregate
        if remaining_output <= 0:
            raise ProbeOutputLimitExceeded("aggregate probe output exceeded limit")
        request = ProcessRequest.fixed(
            argv=argv,
            cwd=cwd,
            timeout_seconds=min(float(policy_timeout), remaining_time),
            output_limit_bytes=min(policy_output_limit, remaining_output),
            executable_fd=executable_fd,
            support_archive_fd=support_archive_fd,
            absolute_deadline=absolute_deadline,
        )
        if tool_environment:
            request = ProcessRequest(
                request.argv,
                request.cwd,
                MappingProxyType(
                    {
                        **FIXED_PROCESS_ENVIRONMENT,
                        "PYTHONPATH": f"/proc/self/fd/{support_archive_fd}",
                    }
                ),
                request.timeout_seconds,
                request.output_limit_bytes,
                request.executable_fd,
                request.support_archive_fd,
                request.absolute_deadline,
            )
        outcome = self._runner.run(request)
        self._ensure_time(absolute_deadline)
        return outcome

    def _ensure_time(self, absolute_deadline: float) -> None:
        if self._monotonic() >= absolute_deadline:
            raise ProbeDeadlineExceeded("probe deadline elapsed during verification")


def _add_raw(current: int, outcome: ProcessOutcome) -> int:
    if not isinstance(outcome.stdout, bytes) or not isinstance(outcome.stderr, bytes):
        raise ProbeCollectorError("process runner returned invalid output")
    total = current + len(outcome.stdout) + len(outcome.stderr)
    if total > AGGREGATE_OUTPUT_LIMIT_BYTES:
        raise ProbeOutputLimitExceeded("aggregate probe output exceeded limit")
    return total


def _add_captured(current: int, captured: int) -> int:
    total = current + captured
    if total > AGGREGATE_OUTPUT_LIMIT_BYTES:
        raise ProbeOutputLimitExceeded("aggregate probe output exceeded limit")
    return total


def _normalize_health(raw: bytes, limit: int) -> Mapping[str, Any]:
    if len(raw) > limit:
        raise ProbeOutputLimitExceeded("health collector output exceeded limit")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProbeCollectorError("health collector output is incompatible") from error
    if not isinstance(document, dict):
        raise ProbeCollectorError("health collector output is incompatible")
    result: dict[str, Any] = {"schema_version": 1}
    captured = _string(document.get("captured_at"), 64)
    if captured is not None:
        result["captured_at"] = captured
    identity = _mapping(document.get("identity"))
    selected_identity = _select(
        identity,
        numbers=(("uptime_seconds", 0, 2**63 - 1),),
    )
    if selected_identity:
        result["identity"] = selected_identity
    for name in ("cpu", "memory", "swap"):
        source = _mapping(document.get(name))
        if name == "cpu":
            selected = _select(
                source,
                numbers=(
                    ("logical_processors", 0, 8192),
                    ("utilization_percent", 0, 100),
                    ("load_1", 0, 1_000_000),
                    ("load_5", 0, 1_000_000),
                    ("load_15", 0, 1_000_000),
                ),
            )
        else:
            selected = _select(
                source,
                numbers=(
                    ("total_bytes", 0, 2**63 - 1),
                    ("available_bytes", 0, 2**63 - 1),
                    ("free_bytes", 0, 2**63 - 1),
                    ("used_bytes", 0, 2**63 - 1),
                    ("used_percent", 0, 100),
                ),
            )
        if selected:
            result[name] = selected
    storage = _select(
        _mapping(document.get("root_filesystem")),
        numbers=(
            ("total_bytes", 0, 2**63 - 1),
            ("available_bytes", 0, 2**63 - 1),
            ("used_bytes", 0, 2**63 - 1),
            ("used_percent", 0, 100),
        ),
        booleans=("read_only",),
    )
    if storage:
        result["storage"] = storage
    accelerator = _select(
        _mapping(document.get("accelerator")),
        strings=(("name", 256), ("driver_version", 64), ("performance_state", 32)),
        numbers=(
            ("utilization_percent", 0, 100),
            ("temperature_c", -100, 300),
            ("power_watts", 0, 100_000),
        ),
        booleans=("available",),
    )
    if accelerator:
        result["accelerator"] = accelerator
    thermal = _thermal(document.get("thermal_zones"))
    if thermal:
        result["thermal"] = thermal
    fabric = _fabric(document.get("fabric"))
    if fabric:
        result["fabric"] = fabric
    runtime = _select(
        _mapping(document.get("services")),
        strings=(("docker_version", 64), ("earlyoom_load_state", 32)),
        booleans=("docker_available", "earlyoom_enabled", "earlyoom_active"),
    )
    if runtime:
        result["runtime"] = runtime
    return _freeze(result)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProbeCollectorError("collector output contains duplicate fields")
        result[key] = value
    return result


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _string(value: Any, maximum: int) -> str | None:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        return None
    if any(ord(character) < 32 for character in value) or "/" in value or "\\" in value:
        return None
    candidate = value.strip("{}")
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}", candidate):
        return None
    if re.fullmatch(r"(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}", value):
        return None
    try:
        ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        pass
    else:
        return None
    return value


def _number(value: Any, minimum: float, maximum: float) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or not minimum <= value <= maximum:
        return None
    return value


def _select(
    source: Mapping[str, Any],
    *,
    strings: tuple[tuple[str, int], ...] = (),
    numbers: tuple[tuple[str, float, float], ...] = (),
    booleans: tuple[str, ...] = (),
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, maximum in strings:
        value = _string(source.get(key), maximum)
        if value is not None:
            result[key] = value
    for key, minimum, maximum in numbers:
        value = _number(source.get(key), minimum, maximum)
        if value is not None:
            result[key] = value
    for key in booleans:
        value = source.get(key)
        if type(value) is bool:
            result[key] = value
    return result


def _thermal(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return result
    for item in value[:64]:
        if not isinstance(item, dict):
            continue
        selected = _select(
            item,
            strings=(("zone", 64), ("type", 64)),
            numbers=(("temperature_c", -100, 300),),
        )
        trips: list[dict[str, Any]] = []
        raw_trips = item.get("trip_points")
        if isinstance(raw_trips, list):
            for raw in raw_trips[:32]:
                if isinstance(raw, dict):
                    trip = _select(
                        raw,
                        strings=(("type", 64),),
                        numbers=(("temperature_c", -100, 300),),
                        booleans=("reached",),
                    )
                    if trip:
                        trips.append(trip)
        if trips:
            selected["trip_points"] = trips
        if selected:
            result.append(selected)
    result.sort(key=lambda item: (item.get("zone", ""), item.get("type", "")))
    return result


_COUNTERS = {
    "out_of_buffer", "out_of_sequence", "duplicate_request", "rnr_nak_retry_err",
    "packet_seq_err", "implied_nak_seq_err", "local_ack_timeout_err",
    "resp_local_length_error", "resp_cqe_error", "req_cqe_error",
    "req_remote_invalid_request", "req_remote_access_errors",
    "resp_remote_access_errors", "resp_cqe_flush_error", "req_cqe_flush_error",
    "req_transport_retries_exceeded", "req_rnr_retries_exceeded",
    "roce_adp_retrans", "roce_adp_retrans_to",
}


def _fabric(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("functions"), list):
        return {}
    functions: list[dict[str, Any]] = []
    for raw in value["functions"][:16]:
        if not isinstance(raw, dict):
            continue
        selected = _select(
            raw,
            strings=(
                ("interface", 64), ("hca", 64), ("operstate", 32),
                ("rdma_interface", 64), ("rdma_state", 32),
            ),
            numbers=(("carrier", 0, 1), ("speed_mbps", 0, 1_000_000), ("mtu", 0, 1_000_000)),
        )
        raw_counters = raw.get("counters")
        counters: dict[str, Any] = {}
        if isinstance(raw_counters, dict):
            for name in sorted(_COUNTERS):
                counter = _number(raw_counters.get(name), 0, 2**63 - 1)
                if counter is not None:
                    counters[name] = counter
        if counters:
            selected["counters"] = counters
        if selected:
            functions.append(selected)
    functions.sort(key=lambda item: (item.get("interface", ""), item.get("hca", "")))
    return {"functions": functions}


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(value[key]) for key in sorted(value)})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value
