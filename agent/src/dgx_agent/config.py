"""Strict, file-backed configuration for the outbound agent."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
from typing import Any
from urllib.parse import urlsplit


DEFAULT_CONFIG_PATH = Path("/etc/dgx-forge-agent/config.json")
DEFAULT_STATE_ROOT = Path("/var/lib/dgx-forge-agent")
MAX_CONFIG_BYTES = 64 * 1024
MAX_IDENTITY_BYTES = 64 * 1024
_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
_REQUIRED_FIELDS = {
    "control_origin",
    "node_id",
    "certificate_path",
    "private_key_path",
    "ca_path",
    "poll_min_seconds",
    "poll_max_seconds",
    "state_root",
    "installed_policy_path",
}


class AgentConfigError(ValueError):
    """Configuration is malformed or violates the agent trust boundary."""


@dataclass(frozen=True)
class AgentConfig:
    """The complete, fixed set of durable agent configuration values."""

    control_origin: str
    node_id: str
    certificate_path: Path
    private_key_path: Path
    ca_path: Path
    poll_min_seconds: int
    poll_max_seconds: int
    state_root: Path
    installed_policy_path: Path

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> "AgentConfig":
        raw = _read_regular_file(Path(path), name="configuration", max_bytes=MAX_CONFIG_BYTES)
        try:
            document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except UnicodeDecodeError as error:
            raise AgentConfigError("configuration must be UTF-8") from error
        except json.JSONDecodeError as error:
            raise AgentConfigError("configuration must be valid JSON") from error
        if not isinstance(document, dict):
            raise AgentConfigError("configuration must be a JSON object")
        if set(document) != _REQUIRED_FIELDS:
            if len(document) != len(set(document)):
                raise AgentConfigError("configuration contains duplicate fields")
            raise AgentConfigError("configuration fields are invalid")

        control_origin = _control_origin(document["control_origin"])
        node_id = _node_id(document["node_id"])
        certificate_path = _absolute_path(document["certificate_path"], name="certificate")
        private_key_path = _absolute_path(document["private_key_path"], name="private key")
        ca_path = _absolute_path(document["ca_path"], name="CA certificate")
        state_root = _absolute_path(document["state_root"], name="state root")
        installed_policy_path = _absolute_path(document["installed_policy_path"], name="installed policy")
        for file_path, name in (
            (certificate_path, "certificate"),
            (private_key_path, "private key"),
            (ca_path, "CA certificate"),
            (installed_policy_path, "installed policy"),
        ):
            _verify_regular_path(file_path, name=name)
        key_mode = stat.S_IMODE(os.stat(private_key_path, follow_symlinks=False).st_mode)
        if key_mode & 0o077:
            raise AgentConfigError("private key permissions are too permissive")
        _verify_path_without_symlinks(state_root, name="state root", allow_missing_leaf=True)
        poll_minimum, poll_maximum = _poll_bounds(
            document["poll_min_seconds"], document["poll_max_seconds"]
        )
        return cls(
            control_origin=control_origin,
            node_id=node_id,
            certificate_path=certificate_path,
            private_key_path=private_key_path,
            ca_path=ca_path,
            poll_min_seconds=poll_minimum,
            poll_max_seconds=poll_maximum,
            state_root=state_root,
            installed_policy_path=installed_policy_path,
        )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AgentConfigError("configuration contains duplicate fields")
        result[key] = value
    return result


def _read_regular_file(path: Path, *, name: str, max_bytes: int) -> bytes:
    _verify_regular_path(path, name=name)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise AgentConfigError(f"{name} cannot be read") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
            raise AgentConfigError(f"{name} is not a regular file or is too large")
        data = os.read(descriptor, max_bytes + 1)
        if len(data) > max_bytes:
            raise AgentConfigError(f"{name} is too large")
        return data
    except OSError as error:
        raise AgentConfigError(f"{name} cannot be read") from error
    finally:
        os.close(descriptor)


def _absolute_path(value: Any, *, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise AgentConfigError(f"{name} path is invalid")
    path = Path(value)
    if not path.is_absolute():
        raise AgentConfigError(f"{name} path must be absolute")
    return path


def _verify_regular_path(path: Path, *, name: str) -> None:
    _verify_path_without_symlinks(path, name=name, allow_missing_leaf=False)
    try:
        mode = os.stat(path, follow_symlinks=False).st_mode
    except OSError as error:
        raise AgentConfigError(f"{name} path is unavailable") from error
    if not stat.S_ISREG(mode):
        raise AgentConfigError(f"{name} must be a regular file")
    if os.stat(path, follow_symlinks=False).st_size > MAX_IDENTITY_BYTES:
        raise AgentConfigError(f"{name} is too large")


def _verify_path_without_symlinks(path: Path, *, name: str, allow_missing_leaf: bool) -> None:
    if not path.is_absolute():
        raise AgentConfigError(f"{name} path must be absolute")
    current = Path("/")
    parts = path.parts[1:]
    for index, part in enumerate(parts):
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError as error:
            if allow_missing_leaf and index == len(parts) - 1:
                return
            raise AgentConfigError(f"{name} path is unavailable") from error
        except OSError as error:
            raise AgentConfigError(f"{name} path is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise AgentConfigError(f"{name} path must not traverse symlinks")
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise AgentConfigError(f"{name} path parent is invalid")


def _control_origin(value: Any) -> str:
    if not isinstance(value, str):
        raise AgentConfigError("control origin is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise AgentConfigError("control origin must be a fixed HTTPS origin")
    return value


def _node_id(value: Any) -> str:
    if not isinstance(value, str) or not _NODE_ID.fullmatch(value):
        raise AgentConfigError("node ID is not canonical")
    return value


def _poll_bounds(minimum: Any, maximum: Any) -> tuple[int, int]:
    if (
        not isinstance(minimum, int)
        or isinstance(minimum, bool)
        or not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or minimum < 1
        or maximum > 300
        or minimum > maximum
    ):
        raise AgentConfigError("poll bounds are invalid")
    return minimum, maximum
