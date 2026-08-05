#!/usr/bin/env python3
"""Run LiteLLM with only an exact, unexpired atomic route bundle."""

from __future__ import annotations

import hashlib
import json
import re
import signal
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path("/routes")
ACTIVATION = ROOT / "activation.json"
GENERATIONS = ROOT / "generations"
BOOTSTRAP = Path("/app/bootstrap-config.json")
POLL_SECONDS = 2
TERMINATE_SECONDS = 30
MAXIMUM_LEASE = timedelta(seconds=300)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_DIRECTORY = re.compile(r"[0-9]{8}-[0-9a-f]{64}\Z")
_MARKER_FIELDS = {
    "schema_version",
    "generation",
    "state",
    "reconciliation_id",
    "plan_digest",
    "evidence_set_digest",
    "routes_sha256",
    "litellm_sha256",
    "issued_at",
    "expires_at",
    "directory",
    "manifest_sha256",
}
_MANIFEST_FIELDS = _MARKER_FIELDS - {"directory", "manifest_sha256"}


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


def _encoded(value: dict[str, object]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _active_config(*, now: datetime) -> Path | None:
    if (
        ACTIVATION.is_symlink()
        or not ACTIVATION.is_file()
        or GENERATIONS.is_symlink()
        or not GENERATIONS.is_dir()
    ):
        return None
    try:
        activation_content = ACTIVATION.read_bytes()
        marker = json.loads(activation_content)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(marker, dict) or set(marker) != _MARKER_FIELDS:
        return None
    if activation_content != _encoded(marker):
        return None
    generation = marker.get("generation")
    directory_name = marker.get("directory")
    manifest_digest = marker.get("manifest_sha256")
    if (
        marker.get("schema_version") != 1
        or marker.get("state") not in {"maintenance", "published"}
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation <= 0
        or not isinstance(directory_name, str)
        or _DIRECTORY.fullmatch(directory_name) is None
        or not isinstance(manifest_digest, str)
        or _DIGEST.fullmatch(manifest_digest) is None
        or directory_name != f"{generation:08d}-{manifest_digest}"
    ):
        return None
    try:
        reconciliation_id = marker.get("reconciliation_id")
        if (
            not isinstance(reconciliation_id, str)
            or str(uuid.UUID(reconciliation_id)) != reconciliation_id
        ):
            return None
    except (ValueError, AttributeError):
        return None
    if any(
        not isinstance(marker.get(field), str)
        or _DIGEST.fullmatch(marker[field]) is None
        for field in (
            "plan_digest",
            "evidence_set_digest",
            "routes_sha256",
            "litellm_sha256",
        )
    ):
        return None
    issued = _parse_timestamp(marker.get("issued_at"))
    expires = _parse_timestamp(marker.get("expires_at"))
    current = now.astimezone(UTC)
    if (
        issued is None
        or expires is None
        or issued > current
        or current >= expires
        or expires <= issued
        or expires - issued > MAXIMUM_LEASE
    ):
        return None
    directory = GENERATIONS / directory_name
    if directory.is_symlink() or not directory.is_dir():
        return None
    manifest = directory / "manifest.json"
    routes = directory / "routes.json"
    config = directory / "litellm.json"
    if any(
        path.is_symlink() or not path.is_file() for path in (manifest, routes, config)
    ):
        return None
    try:
        exact_manifest = {field: marker[field] for field in _MANIFEST_FIELDS}
        config_document = json.loads(config.read_bytes())
    except (OSError, KeyError, json.JSONDecodeError):
        return None
    if (
        manifest.read_bytes() != _encoded(exact_manifest)
        or _digest(manifest) != manifest_digest
        or _digest(routes) != marker["routes_sha256"]
        or _digest(config) != marker["litellm_sha256"]
        or not isinstance(config_document, dict)
        or not isinstance(config_document.get("model_list"), list)
        or (marker["state"] == "maintenance" and config_document["model_list"] != [])
    ):
        return None
    return config


def _selected(*, now: datetime | None = None) -> Path:
    active = _active_config(now=now or datetime.now(UTC))
    if active is not None:
        return active
    if BOOTSTRAP.is_symlink() or not BOOTSTRAP.is_file():
        raise RuntimeError("LiteLLM bootstrap config is unavailable")
    try:
        document = json.loads(BOOTSTRAP.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("LiteLLM bootstrap config is invalid") from error
    if not isinstance(document, dict) or document.get("model_list") != []:
        raise RuntimeError("LiteLLM bootstrap config must be empty")
    return BOOTSTRAP


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
