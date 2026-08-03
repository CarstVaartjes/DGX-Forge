"""Central structured redaction and content-addressed operational logs."""

from __future__ import annotations

import hashlib
import json
import logging as stdlib_logging
import os
import re
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

_SENSITIVE_KEY = re.compile(r"(?i)(authorization|api.?key|password|secret|token|private.?key|credential)")
_AUTHORIZATION = re.compile(r"(?i)authorization\s*:\s*(?:bearer|basic)\s+[^\s,;]+")
_BEARER = re.compile(r"(?i)\b(?:bearer|basic)\s+[^\s,;]+")
_ASSIGNMENT = re.compile(r"(?i)\b(password|secret|token|api[_-]?key)\s*[:=]\s*[^\s,;]+")
_PRIVATE_BLOCK = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.DOTALL)
_DIGEST = re.compile(r"[0-9a-f]{64}")
_MAX_FIELD = 4096
_MAX_LOG_INPUT = 1_048_576


def redact_text(value: object) -> str:
    text = str(value).replace("\x00", "")
    text = _PRIVATE_BLOCK.sub("<redacted>", text)
    text = _AUTHORIZATION.sub("Authorization: <redacted>", text)
    text = _BEARER.sub("<redacted>", text)
    text = _ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    if len(text) > _MAX_FIELD:
        text = text[: _MAX_FIELD - len("<truncated>")] + "<truncated>"
    return text


def _safe(value: object, key: str = "") -> object:
    if _SENSITIVE_KEY.search(key):
        return "<redacted>"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(child_key): _safe(child, str(child_key)) for child_key, child in list(value.items())[:64]}
    if isinstance(value, (list, tuple)):
        return [_safe(child) for child in value[:64]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(value)


def log_event(logger: stdlib_logging.Logger, event: str, *, service: str, **fields: object) -> None:
    if re.fullmatch(r"[a-z][a-z0-9_.-]{1,127}", event) is None:
        raise ValueError("structured log event name is invalid")
    if re.fullmatch(r"[a-z][a-z0-9_.-]{1,63}", service) is None:
        raise ValueError("structured log service name is invalid")
    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "level": stdlib_logging.getLevelName(logger.getEffectiveLevel()).lower(),
        "service": service,
        "event": event,
        **{key: _safe(value, key) for key, value in fields.items()},
    }
    logger.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))


class JobLogStore:
    def __init__(self, root: Path) -> None:
        if root.is_symlink():
            raise ValueError("job log root must not be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        self._root = root

    @staticmethod
    def _job_id(job_id: str) -> str:
        try:
            parsed = uuid.UUID(job_id)
        except ValueError:
            raise ValueError("job log ID must be a UUID") from None
        if str(parsed) != job_id:
            raise ValueError("job log ID must use canonical UUID form")
        return job_id

    def save(self, job_id: str, content: bytes) -> str:
        identity = self._job_id(job_id)
        if not isinstance(content, bytes) or len(content) > _MAX_LOG_INPUT:
            raise ValueError("job log input is invalid or too large")
        sanitized = redact_text(content.decode("utf-8", errors="replace")).encode() + b"\n"
        digest = hashlib.sha256(sanitized).hexdigest()
        directory = self._root / identity
        if directory.is_symlink():
            raise ValueError("job log directory must not be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        target = directory / f"{digest}.log"
        descriptor, temporary_raw = tempfile.mkstemp(prefix=".job-log-", dir=directory)
        temporary = Path(temporary_raw)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(sanitized); output.flush(); os.fsync(output.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.is_symlink() or target.read_bytes() != sanitized:
                    raise ValueError("existing job log conflicts") from None
            target.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)
        return digest

    def list(self, job_id: str) -> tuple[str, ...]:
        directory = self._root / self._job_id(job_id)
        if not directory.exists():
            return ()
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("job log directory is unsafe")
        return tuple(sorted(path.stem for path in directory.glob("*.log") if _DIGEST.fullmatch(path.stem)))

    def read(self, job_id: str, digest: str) -> bytes:
        if _DIGEST.fullmatch(digest) is None:
            raise ValueError("job log digest is invalid")
        target = self._root / self._job_id(job_id) / f"{digest}.log"
        if target.is_symlink() or not target.is_file():
            raise KeyError(digest)
        content = target.read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("job log checksum mismatch")
        return content
