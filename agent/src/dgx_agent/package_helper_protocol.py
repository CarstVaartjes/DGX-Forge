"""Canonical, bounded protocol for the root-owned package helper."""

from __future__ import annotations

import hashlib
import json
import re
import socket
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .packages.backends import BackendInvocation, BackendValidationError

MAX_HELPER_MESSAGE_BYTES = 256 * 1024
MAX_RECEIPTS = 256
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SIGNATURE = re.compile(r"[A-Za-z0-9_-]{86}\Z")
_TOKEN = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_UTC_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")


class HelperProtocolError(ValueError):
    """The helper message or authorization is invalid."""


@dataclass(frozen=True)
class SignedObjectReceipt:
    schema_version: int
    object_digest: str
    size: int
    relative_name: str
    signature: str

    def __post_init__(self) -> None:
        if self.schema_version != 1 or isinstance(self.schema_version, bool):
            raise HelperProtocolError("receipt schema version is invalid")
        _digest(self.object_digest, "receipt object digest")
        if (
            not isinstance(self.size, int)
            or isinstance(self.size, bool)
            or not 1 <= self.size <= 2**63 - 1
        ):
            raise HelperProtocolError("receipt size is invalid")
        expected = f"objects/sha256/{self.object_digest}"
        if self.relative_name != expected:
            raise HelperProtocolError("receipt relative name is invalid")
        if not isinstance(self.signature, str) or not _SIGNATURE.fullmatch(
            self.signature
        ):
            raise HelperProtocolError("receipt signature is invalid")

    @classmethod
    def parse(cls, value: object) -> SignedObjectReceipt:
        document = _object(
            value,
            {
                "schema_version",
                "object_digest",
                "size",
                "relative_name",
                "signature",
            },
            "receipt fields",
        )
        return cls(
            document["schema_version"],
            document["object_digest"],
            document["size"],
            document["relative_name"],
            document["signature"],
        )

    def unsigned_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "object_digest": self.object_digest,
            "size": self.size,
            "relative_name": self.relative_name,
        }

    def to_mapping(self) -> dict[str, object]:
        return self.unsigned_mapping() | {"signature": self.signature}


@dataclass(frozen=True)
class HelperRequest:
    schema_version: int
    request_id: str
    job_id: str
    operation_id: str
    attempt: int
    fence: str
    invocation: BackendInvocation
    receipts: tuple[SignedObjectReceipt, ...]
    expires_at: str
    authorization: str

    def __post_init__(self) -> None:
        if self.schema_version != 1 or isinstance(self.schema_version, bool):
            raise HelperProtocolError("helper request version is invalid")
        for value, name in (
            (self.request_id, "request ID"),
            (self.job_id, "job ID"),
            (self.operation_id, "operation ID"),
            (self.fence, "operation fence"),
        ):
            _uuid(value, name)
        if (
            not isinstance(self.attempt, int)
            or isinstance(self.attempt, bool)
            or not 1 <= self.attempt <= 2**31 - 1
        ):
            raise HelperProtocolError("request attempt is invalid")
        if type(self.invocation) is not BackendInvocation:
            raise HelperProtocolError("backend invocation is invalid")
        if (
            not 1 <= len(self.receipts) <= MAX_RECEIPTS
            or not all(
                type(receipt) is SignedObjectReceipt for receipt in self.receipts
            )
            or len({receipt.object_digest for receipt in self.receipts})
            != len(self.receipts)
        ):
            raise HelperProtocolError("object receipts are invalid")
        if not isinstance(self.authorization, str) or not _SIGNATURE.fullmatch(
            self.authorization
        ):
            raise HelperProtocolError("helper request authorization is invalid")
        _expiry(self.expires_at)
        receipt_digests = {receipt.object_digest for receipt in self.receipts}
        if not {mount.object_digest for mount in self.invocation.mounts}.issubset(
            receipt_digests
        ):
            raise HelperProtocolError("mount has no signed object receipt")

    @classmethod
    def parse(cls, raw: bytes) -> HelperRequest:
        document = _parse_canonical(raw, "helper request")
        document = _object(
            document,
            {
                "schema_version",
                "request_id",
                "job_id",
                "operation_id",
                "attempt",
                "fence",
                "invocation",
                "receipts",
                "authorization",
                "expires_at",
            },
            "helper request fields",
        )
        receipts = document["receipts"]
        if not isinstance(receipts, list) or not 1 <= len(receipts) <= MAX_RECEIPTS:
            raise HelperProtocolError("object receipts are invalid")
        try:
            invocation = BackendInvocation.parse(document["invocation"])
        except BackendValidationError as error:
            raise HelperProtocolError("backend invocation is invalid") from error
        return cls(
            document["schema_version"],
            document["request_id"],
            document["job_id"],
            document["operation_id"],
            document["attempt"],
            document["fence"],
            invocation,
            tuple(SignedObjectReceipt.parse(item) for item in receipts),
            document["expires_at"],
            document["authorization"],
        )

    def unsigned_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "request_id": self.request_id,
            "job_id": self.job_id,
            "operation_id": self.operation_id,
            "attempt": self.attempt,
            "fence": self.fence,
            "invocation": self.invocation.to_mapping(),
            "receipts": [receipt.to_mapping() for receipt in self.receipts],
            "expires_at": self.expires_at,
        }

    def to_mapping(self) -> dict[str, object]:
        return self.unsigned_mapping() | {"authorization": self.authorization}

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_helper_document(self.to_mapping())).hexdigest()


@dataclass(frozen=True)
class HelperResponse:
    schema_version: int
    request_id: str
    status: str
    evidence_digest: str
    fence: str

    def __post_init__(self) -> None:
        if self.schema_version != 1 or isinstance(self.schema_version, bool):
            raise HelperProtocolError("helper response version is invalid")
        _uuid(self.request_id, "request ID")
        if not isinstance(self.status, str) or not _TOKEN.fullmatch(self.status):
            raise HelperProtocolError("helper response status is invalid")
        _digest(self.evidence_digest, "helper evidence digest")
        _uuid(self.fence, "operation fence")

    @classmethod
    def parse(cls, raw: bytes) -> HelperResponse:
        document = _object(
            _parse_canonical(raw, "helper response"),
            {"schema_version", "request_id", "status", "evidence_digest", "fence"},
            "helper response fields",
        )
        return cls(
            document["schema_version"],
            document["request_id"],
            document["status"],
            document["evidence_digest"],
            document["fence"],
        )

    def to_bytes(self) -> bytes:
        return canonical_helper_document(
            {
                "schema_version": 1,
                "request_id": self.request_id,
                "status": self.status,
                "evidence_digest": self.evidence_digest,
                "fence": self.fence,
            }
        )


def canonical_helper_document(value: Mapping[str, object]) -> bytes:
    try:
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise HelperProtocolError("helper document is not JSON") from error
    if len(raw) > MAX_HELPER_MESSAGE_BYTES:
        raise HelperProtocolError("helper document exceeds its bound")
    return raw


def frame_helper_message(raw: bytes) -> bytes:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_HELPER_MESSAGE_BYTES:
        raise HelperProtocolError("helper message exceeds its bound")
    return struct.pack(">I", len(raw)) + raw


def receive_helper_message(
    connection: socket.socket, *, timeout_seconds: float = 5.0
) -> bytes:
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not 0 < timeout_seconds <= 30
    ):
        raise HelperProtocolError("helper connection deadline is invalid")
    previous = connection.gettimeout()
    connection.settimeout(float(timeout_seconds))
    try:
        header = _receive_exact(connection, 4)
        (size,) = struct.unpack(">I", header)
        if not 1 <= size <= MAX_HELPER_MESSAGE_BYTES:
            raise HelperProtocolError("helper message exceeds its bound")
        return _receive_exact(connection, size)
    except (OSError, struct.error) as error:
        raise HelperProtocolError(
            "helper connection failed within its deadline"
        ) from error
    finally:
        connection.settimeout(previous)


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise HelperProtocolError("helper message is truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _parse_canonical(raw: bytes, name: str) -> Mapping[str, Any]:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_HELPER_MESSAGE_BYTES:
        raise HelperProtocolError(f"{name} exceeds its bound")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except HelperProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HelperProtocolError(f"{name} is not valid JSON") from error
    if not isinstance(document, Mapping):
        raise HelperProtocolError(f"{name} must be an object")
    if canonical_helper_document(document) != raw:
        raise HelperProtocolError(f"{name} is not canonical")
    return document


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HelperProtocolError(f"duplicate helper field: {key}")
        result[key] = value
    return result


def _object(value: object, fields: set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise HelperProtocolError(f"{name} are invalid")
    if not all(isinstance(key, str) for key in value):
        raise HelperProtocolError(f"{name} are invalid")
    return value


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise HelperProtocolError(f"{name} is invalid")
    return value


def _uuid(value: object, name: str) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise HelperProtocolError(f"{name} is invalid")
    return value


def _expiry(value: object) -> datetime:
    if not isinstance(value, str) or not _UTC_TIMESTAMP.fullmatch(value):
        raise HelperProtocolError("helper request expiry is invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise HelperProtocolError("helper request expiry is invalid") from error
    return parsed
