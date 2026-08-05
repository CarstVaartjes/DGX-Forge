#!/usr/bin/env python3
"""Restart LiteLLM only when an atomically published route config changes."""

from __future__ import annotations

import hashlib
import json
import signal
import subprocess
import time
from pathlib import Path

GENERATED = Path("/routes/config.yaml")
BOOTSTRAP = Path("/app/bootstrap-config.yaml")
POLL_SECONDS = 2
TERMINATE_SECONDS = 30


def _generated_is_valid() -> bool:
    if GENERATED.is_symlink() or not GENERATED.is_file():
        return False
    try:
        document = json.loads(GENERATED.read_bytes())
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(document, dict) and isinstance(document.get("model_list"), list)


def _selected() -> Path:
    if _generated_is_valid():
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
