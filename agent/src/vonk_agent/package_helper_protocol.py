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

from vonk_agent_protocol import AgentProtocolError
from vonk_agent_protocol.workload_packages import (
    PackageHelperOperation,
    SignedPackageHelperGrant,
    SignedPackageObjectReceipt,
)

from .packages.backends import BackendInvocation, BackendValidationError

MAX_HELPER_MESSAGE_BYTES = 256 * 1024
MAX_RECEIPTS = 256
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_UTC_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")


class HelperProtocolError(ValueError):
    """The helper message or authorization is invalid."""


SignedObjectReceipt = SignedPackageObjectReceipt


@dataclass(frozen=True)
class HelperExecutionBody:
    schema_version: int
    request_id: str
    node_id: str
    job_id: str
    operation_id: str
    attempt: int
    fence: str
    operation: PackageHelperOperation
    invocation: BackendInvocation
    receipts: tuple[SignedPackageObjectReceipt, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1 or isinstance(self.schema_version, bool):
            raise HelperProtocolError("helper execution body version is invalid")
        for value, name in (
            (self.request_id, "request ID"),
            (self.job_id, "job ID"),
            (self.operation_id, "operation ID"),
            (self.fence, "operation fence"),
        ):
            _uuid(value, name)
        if (
            not isinstance(self.node_id, str)
            or re.fullmatch(r"spk_[0-9a-f]{32}", self.node_id) is None
        ):
            raise HelperProtocolError("helper node ID is invalid")
        if (
            not isinstance(self.attempt, int)
            or isinstance(self.attempt, bool)
            or not 1 <= self.attempt <= 2**31 - 1
        ):
            raise HelperProtocolError("request attempt is invalid")
        if type(self.operation) is not PackageHelperOperation:
            raise HelperProtocolError("helper workload operation is invalid")
        if type(self.invocation) is not BackendInvocation:
            raise HelperProtocolError("backend invocation is invalid")
        if (
            not 1 <= len(self.receipts) <= MAX_RECEIPTS
            or not all(
                type(receipt) is SignedPackageObjectReceipt for receipt in self.receipts
            )
            or len({receipt.claims.object_digest for receipt in self.receipts})
            != len(self.receipts)
        ):
            raise HelperProtocolError("object receipts are invalid")
        receipt_digests = {receipt.claims.object_digest for receipt in self.receipts}
        if not {mount.object_digest for mount in self.invocation.mounts}.issubset(
            receipt_digests
        ):
            raise HelperProtocolError("mount has no signed object receipt")

    @classmethod
    def parse(cls, value: object) -> HelperExecutionBody:
        document = _object(
            value,
            {
                "schema_version",
                "request_id",
                "node_id",
                "job_id",
                "operation_id",
                "attempt",
                "fence",
                "operation",
                "invocation",
                "receipts",
            },
            "helper execution body fields",
        )
        receipts = document["receipts"]
        if not isinstance(receipts, list) or not 1 <= len(receipts) <= MAX_RECEIPTS:
            raise HelperProtocolError("object receipts are invalid")
        try:
            invocation = BackendInvocation.parse(document["invocation"])
        except BackendValidationError as error:
            raise HelperProtocolError("backend invocation is invalid") from error
        try:
            operation = PackageHelperOperation(document["operation"])
            parsed_receipts = tuple(
                SignedPackageObjectReceipt.parse(item) for item in receipts
            )
        except (AgentProtocolError, TypeError, ValueError) as error:
            raise HelperProtocolError("helper authority body is invalid") from error
        return cls(
            document["schema_version"],
            document["request_id"],
            document["node_id"],
            document["job_id"],
            document["operation_id"],
            document["attempt"],
            document["fence"],
            operation,
            invocation,
            parsed_receipts,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "request_id": self.request_id,
            "node_id": self.node_id,
            "job_id": self.job_id,
            "operation_id": self.operation_id,
            "attempt": self.attempt,
            "fence": self.fence,
            "operation": self.operation.value,
            "invocation": self.invocation.to_mapping(),
            "receipts": [receipt.to_mapping() for receipt in self.receipts],
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_helper_document(self.to_mapping())).hexdigest()


@dataclass(frozen=True)
class HelperRequest:
    body: HelperExecutionBody
    grant: SignedPackageHelperGrant

    def __post_init__(self) -> None:
        if type(self.body) is not HelperExecutionBody or type(
            self.grant
        ) is not SignedPackageHelperGrant:
            raise HelperProtocolError("helper request is invalid")
        claims = self.grant.claims
        expected = (
            self.body.request_id,
            self.body.node_id,
            self.body.job_id,
            self.body.operation_id,
            self.body.attempt,
            self.body.fence,
            self.body.invocation.release_digest,
            self.body.invocation.generation,
            self.body.operation,
            self.body.digest,
        )
        actual = (
            claims.request_id,
            claims.node_id,
            claims.job_id,
            claims.operation_id,
            claims.attempt,
            claims.fence,
            claims.release_digest,
            claims.generation,
            claims.operation,
            claims.request_digest,
        )
        if actual != expected:
            raise HelperProtocolError("helper grant does not bind execution body")

    @classmethod
    def parse(cls, raw: bytes) -> HelperRequest:
        document = _object(
            _parse_canonical(raw, "helper request"),
            {"schema_version", "body", "grant"},
            "helper request fields",
        )
        if document["schema_version"] != 1 or isinstance(
            document["schema_version"], bool
        ):
            raise HelperProtocolError("helper request version is invalid")
        try:
            grant = SignedPackageHelperGrant.parse(document["grant"])
        except (AgentProtocolError, TypeError, ValueError) as error:
            raise HelperProtocolError("helper grant is invalid") from error
        return cls(HelperExecutionBody.parse(document["body"]), grant)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "body": self.body.to_mapping(),
            "grant": self.grant.to_mapping(),
        }

    def to_bytes(self) -> bytes:
        return canonical_helper_document(self.to_mapping())

    @property
    def digest(self) -> str:
        return self.body.digest

    @property
    def request_id(self) -> str:
        return self.body.request_id

    @property
    def node_id(self) -> str:
        return self.body.node_id

    @property
    def job_id(self) -> str:
        return self.body.job_id

    @property
    def operation_id(self) -> str:
        return self.body.operation_id

    @property
    def attempt(self) -> int:
        return self.body.attempt

    @property
    def fence(self) -> str:
        return self.body.fence

    @property
    def operation(self) -> PackageHelperOperation:
        return self.body.operation

    @property
    def invocation(self) -> BackendInvocation:
        return self.body.invocation

    @property
    def receipts(self) -> tuple[SignedPackageObjectReceipt, ...]:
        return self.body.receipts


@dataclass(frozen=True)
class HelperResponse:
    schema_version: int
    request_id: str
    status: str
    evidence_digest: str
    fence: str
    request_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != 1 or isinstance(self.schema_version, bool):
            raise HelperProtocolError("helper response version is invalid")
        _uuid(self.request_id, "request ID")
        if not isinstance(self.status, str) or not _TOKEN.fullmatch(self.status):
            raise HelperProtocolError("helper response status is invalid")
        _digest(self.evidence_digest, "helper evidence digest")
        _uuid(self.fence, "operation fence")
        _digest(self.request_digest, "helper request digest")

    @classmethod
    def parse(cls, raw: bytes) -> HelperResponse:
        document = _object(
            _parse_canonical(raw, "helper response"),
            {
                "schema_version",
                "request_id",
                "status",
                "evidence_digest",
                "fence",
                "request_digest",
            },
            "helper response fields",
        )
        return cls(
            document["schema_version"],
            document["request_id"],
            document["status"],
            document["evidence_digest"],
            document["fence"],
            document["request_digest"],
        )

    def to_bytes(self) -> bytes:
        return canonical_helper_document(
            {
                "schema_version": 1,
                "request_id": self.request_id,
                "status": self.status,
                "evidence_digest": self.evidence_digest,
                "fence": self.fence,
                "request_digest": self.request_digest,
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
