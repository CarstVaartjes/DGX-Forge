"""Small shared redaction boundary for persisted operational messages."""

from __future__ import annotations

import re

_AUTHORIZATION = re.compile(
    r"(?i)(authorization\s*:\s*)(?:bearer|basic)\s+[^\s,;]+"
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(password|private[_-]?key|secret|token)\b(\s*[:=]\s*)[^\s,;]+"
)


def redact_message(value: str) -> str:
    redacted = _AUTHORIZATION.sub(r"\1[REDACTED]", value)
    return _SENSITIVE_ASSIGNMENT.sub(r"\1\2[REDACTED]", redacted)
