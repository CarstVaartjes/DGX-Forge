"""Stable-cardinality, content-free OpenMetrics exporter."""

from __future__ import annotations

import math
import re
import threading
from collections import defaultdict

_NODE = re.compile(r"spk_[0-9a-f]{32}")
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_JOB_KINDS = frozenset({"install", "probe", "reconcile", "deploy", "backup", "restore"})
_JOB_STATES = frozenset({"queued", "running", "waiting-for-operator", "succeeded", "failed", "expired"})
_ROUTE_STATES = frozenset({"published", "maintenance", "unavailable"})
_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._nodes: dict[str, tuple[bool, int, int, float]] = {}
        self._jobs: dict[tuple[str, str], int] = {}
        self._route_state = "unavailable"
        self._backup_age: float | None = None
        self._api_counts: dict[tuple[str, str], int] = defaultdict(int)
        self._api_durations: dict[tuple[str, str], list[float]] = defaultdict(list)

    @staticmethod
    def _number(value: int | float, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or not math.isfinite(value):
            raise ValueError(f"{field} must be a nonnegative finite number")
        return float(value)

    def update_node(self, node_id: str, *, ready: bool, memory_available_bytes: int, disk_available_bytes: int, probe_age_seconds: float) -> None:
        if _NODE.fullmatch(node_id) is None:
            raise ValueError("metrics node ID must be a stable generated ID")
        if not isinstance(ready, bool):
            raise ValueError("node readiness must be boolean")
        memory = int(self._number(memory_available_bytes, "memory"))
        disk = int(self._number(disk_available_bytes, "disk"))
        age = self._number(probe_age_seconds, "probe age")
        with self._lock:
            self._nodes[node_id] = (ready, memory, disk, age)

    def set_job_count(self, kind: str, state: str, count: int) -> None:
        safe_kind = kind if kind in _JOB_KINDS else "other"
        safe_state = state if state in _JOB_STATES else "other"
        bounded = int(self._number(count, "job count"))
        with self._lock:
            self._jobs[(safe_kind, safe_state)] = bounded

    def set_route_state(self, state: str) -> None:
        if state not in _ROUTE_STATES:
            raise ValueError("route metric state is invalid")
        with self._lock:
            self._route_state = state

    def set_backup_age(self, age_seconds: float) -> None:
        age = self._number(age_seconds, "backup age")
        with self._lock:
            self._backup_age = age

    def observe_api(self, method: str, status_code: int, duration_seconds: float) -> None:
        safe_method = method if method in _METHODS else "OTHER"
        status_class = f"{status_code // 100}xx" if 100 <= status_code <= 599 else "other"
        duration = self._number(duration_seconds, "API duration")
        with self._lock:
            key = (safe_method, status_class)
            self._api_counts[key] += 1
            self._api_durations[key].append(duration)

    def render(self) -> str:
        with self._lock:
            nodes, jobs = dict(self._nodes), dict(self._jobs)
            route_state = self._route_state
            backup_age = self._backup_age
            api_counts = dict(self._api_counts)
            api_durations = {key: tuple(values) for key, values in self._api_durations.items()}
        lines = [
            "# HELP dgx_route_state Current inference route state.",
            "# TYPE dgx_route_state gauge",
        ]
        for state in sorted(_ROUTE_STATES):
            lines.append(f'dgx_route_state{{state="{state}"}} {1 if state == route_state else 0}')
        if backup_age is not None:
            lines.extend((
                "# HELP dgx_control_backup_age_seconds Age of the last successful encrypted control backup.",
                "# TYPE dgx_control_backup_age_seconds gauge",
                f"dgx_control_backup_age_seconds {backup_age:g}",
            ))
        lines.extend(("# HELP dgx_node_ready Whether the stable fleet node is ready.", "# TYPE dgx_node_ready gauge"))
        for node_id, (ready, memory, disk, age) in sorted(nodes.items()):
            label = f'node_id="{node_id}"'
            lines.extend((
                f"dgx_node_ready{{{label}}} {1 if ready else 0}",
                f"dgx_node_memory_available_bytes{{{label}}} {memory}",
                f"dgx_node_disk_available_bytes{{{label}}} {disk}",
                f"dgx_node_probe_age_seconds{{{label}}} {age:g}",
            ))
        lines.extend(("# HELP dgx_jobs Number of control jobs by bounded kind and state.", "# TYPE dgx_jobs gauge"))
        for (kind, state), count in sorted(jobs.items()):
            lines.append(f'dgx_jobs{{kind="{kind}",state="{state}"}} {count}')
        lines.extend(("# HELP dgx_api_requests_total API responses by method and status class.", "# TYPE dgx_api_requests_total counter"))
        for (method, status_class), count in sorted(api_counts.items()):
            labels = f'method="{method}",status_class="{status_class}"'
            lines.append(f"dgx_api_requests_total{{{labels}}} {count}")
            values = api_durations[(method, status_class)]
            cumulative = 0
            for bucket in _BUCKETS:
                cumulative = sum(value <= bucket for value in values)
                lines.append(f'dgx_api_request_duration_seconds_bucket{{{labels},le="{bucket:g}"}} {cumulative}')
            lines.append(f'dgx_api_request_duration_seconds_bucket{{{labels},le="+Inf"}} {len(values)}')
            lines.append(f"dgx_api_request_duration_seconds_sum{{{labels}}} {sum(values):g}")
            lines.append(f"dgx_api_request_duration_seconds_count{{{labels}}} {len(values)}")
        lines.append("# EOF")
        return "\n".join(lines) + "\n"
