#!/usr/bin/env python3
"""Restart LiteLLM only when an atomically published route config changes."""

from __future__ import annotations

import hashlib
import json
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

GENERATED = Path("/routes/config.yaml")
LEASE = Path("/routes/lease.json")
BOOTSTRAP = Path("/app/bootstrap-config.yaml")
POLL_SECONDS = 2
TERMINATE_SECONDS = 30
STARTED_AT = datetime.now(UTC)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _generated_is_valid(*, now: datetime | None = None) -> bool:
    if (
        GENERATED.is_symlink()
        or not GENERATED.is_file()
        or LEASE.is_symlink()
        or not LEASE.is_file()
    ):
        return False
    try:
        content = GENERATED.read_bytes()
        document = json.loads(content)
        lease = json.loads(LEASE.read_bytes())
    except (OSError, json.JSONDecodeError):
        return False
    if (
        not isinstance(document, dict)
        or not isinstance(document.get("model_list"), list)
        or not isinstance(lease, dict)
        or set(lease) != {"config_sha256", "issued_at", "expires_at"}
        or lease.get("config_sha256") != hashlib.sha256(content).hexdigest()
    ):
        return False
    issued_at = _parse_timestamp(lease.get("issued_at"))
    expires_at = _parse_timestamp(lease.get("expires_at"))
    current = (now or datetime.now(UTC)).astimezone(UTC)
    return (
        issued_at is not None
        and expires_at is not None
        and issued_at >= STARTED_AT.astimezone(UTC)
        and issued_at <= current < expires_at
    )


def _selected(*, now: datetime | None = None) -> Path:
    if _generated_is_valid(now=now):
        return GENERATED
    if BOOTSTRAP.is_symlink() or not BOOTSTRAP.is_file():
        raise RuntimeError("LiteLLM bootstrap config is unavailable")
    return BOOTSTRAP


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stop(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=TERMINATE_SECONDS)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def main() -> int:
    stopping = False
    child: subprocess.Popen[bytes] | None = None

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        if child is not None:
            child.terminate()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    selected = _selected()
    while not stopping:
        active_digest = _digest(selected)
        child = subprocess.Popen(
            [
                "litellm",
                "--config",
                str(selected),
                "--host",
                "0.0.0.0",
                "--port",
                "4000",
            ],
            stdin=subprocess.DEVNULL,
        )
        reload_requested = False
        while child.poll() is None and not stopping:
            time.sleep(POLL_SECONDS)
            candidate = _selected()
            if candidate != selected or _digest(candidate) != active_digest:
                reload_requested = True
                _stop(child)
                selected = candidate
                break
        if stopping:
            _stop(child)
            return 0
        if reload_requested:
            continue
        return int(child.returncode or 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
