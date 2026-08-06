from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from .contracts import AgentProtocolError, canonical_message

MAX_RELEASE_LOCK_BYTES = 1024 * 1024
MAX_COMPONENT_SIZE = 2**63 - 1
MAX_DEPENDENCY_DEPTH = 8
MAX_AGGREGATE_COMPONENTS = 256
MAX_SOURCES = 8
MAX_EVIDENCE = 16
MAX_PACKAGE_HELPER_GRANT_SECONDS = 15 * 60

PACKAGE_HELPER_AUTHORITY = "dgx.workload-package-helper"
PACKAGE_HELPER_GRANT_DOMAIN = b"DGX-WORKLOAD-PACKAGE-HELPER-GRANT-V1\0"
PACKAGE_OBJECT_RECEIPT_DOMAIN = b"DGX-WORKLOAD-PACKAGE-OBJECT-RECEIPT-V1\0"

IDENTIFIER = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
PLATFORM = re.compile(r"[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*\Z")
MEDIA_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
CONTENT_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
ED25519_SIGNATURE = re.compile(r"[0-9a-f]{128}\Z")
GIT_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
OCI_REFERENCE = re.compile(r"[a-z0-9][a-z0-9._:/-]{0,510}@sha256:[0-9a-f]{64}\Z")
HF_REPOSITORY = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,95})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,95})\Z"
)
UNSAFE_FIELD = re.compile(
    r"password|secret|token|authorization|private.?key|command|shell|"
    r"(?:^|[_-])(?:path|file|filename|filepath|directory|folder)(?:$|[_-])|"
    r"host.?path|environment",
    re.IGNORECASE,
)


def _duplicate_free_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AgentProtocolError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_document(value: Any) -> Mapping[str, Any]:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    elif isinstance(value, Mapping):
        return value
    else:
        raise AgentProtocolError("workload release lock must be JSON or an object")
    if len(raw) > MAX_RELEASE_LOCK_BYTES:
        raise AgentProtocolError("workload release lock is too large")
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_duplicate_free_object,
        )
    except AgentProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AgentProtocolError(
            "workload release lock is not valid UTF-8 JSON"
        ) from error
    if not isinstance(document, Mapping):
        raise AgentProtocolError("workload release lock must be a JSON object")
    return document


def _mapping(value: Any, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentProtocolError(f"{name} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise AgentProtocolError(f"{name} keys must be strings")
    return value


def _exact_fields(
    value: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str] = frozenset(),
    name: str,
) -> None:
    missing = required - set(value)
    unknown = set(value) - required - optional
    if missing:
        raise AgentProtocolError(f"{name} missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise AgentProtocolError(f"{name} unknown fields: {', '.join(sorted(unknown))}")
    for key in value:
        if UNSAFE_FIELD.search(key):
            raise AgentProtocolError(f"{name} contains unsafe field: {key}")


def _identifier(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise AgentProtocolError(f"{name} must be a canonical identifier")
    return value


def _bounded_text(value: Any, *, name: str, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or value != value.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or "\\" in value
    ):
        raise AgentProtocolError(f"{name} is not bounded canonical text")
    return value


def _positive_integer(value: Any, *, name: str, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= maximum
    ):
        raise AgentProtocolError(f"{name} must be a bounded positive integer")
    return value


def _uuid4(value: Any, *, name: str) -> str:
    if not isinstance(value, str):
        raise AgentProtocolError(f"{name} must be a canonical UUIDv4")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise AgentProtocolError(f"{name} must be a canonical UUIDv4") from error
    if parsed.version != 4 or str(parsed) != value:
        raise AgentProtocolError(f"{name} must be a canonical UUIDv4")
    return value


def _sha256(value: Any, *, name: str, prefixed: bool) -> str:
    pattern = CONTENT_DIGEST if prefixed else SHA256
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        label = "sha256:<64 lowercase hex>" if prefixed else "64 lowercase hex"
        raise AgentProtocolError(f"{name} must be {label}")
    return value


def _https_url(value: Any, *, name: str) -> str:
    text = _bounded_text(value, name=name, maximum=2048)
    parsed = urlsplit(text)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AgentProtocolError(
            f"{name} must be an HTTPS URL without credentials or query data"
        )
    return text


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _sequence(
    value: Any,
    *,
    name: str,
    minimum: int = 0,
    maximum: int,
) -> Sequence[Any]:
    if (
        not isinstance(value, (list, tuple))
        or isinstance(value, (str, bytes))
        or not minimum <= len(value) <= maximum
    ):
        raise AgentProtocolError(
            f"{name} must contain between {minimum} and {maximum} items"
        )
    return value


def _parse_source(value: Any) -> Mapping[str, object]:
    source = _mapping(value, name="component source")
    provider = source.get("provider")
    if provider == "https":
        _exact_fields(source, required={"provider", "url"}, name="component source")
        parsed = {
            "provider": provider,
            "url": _https_url(source["url"], name="source URL"),
        }
    elif provider == "oci":
        _exact_fields(
            source,
            required={"provider", "reference"},
            name="component source",
        )
        reference = source["reference"]
        if not isinstance(reference, str) or OCI_REFERENCE.fullmatch(reference) is None:
            raise AgentProtocolError("OCI source must use an exact digest reference")
        parsed = {"provider": provider, "reference": reference}
    elif provider == "git":
        _exact_fields(
            source,
            required={"provider", "repository", "commit"},
            name="component source",
        )
        commit = source["commit"]
        if not isinstance(commit, str) or GIT_COMMIT.fullmatch(commit) is None:
            raise AgentProtocolError("Git source commit must be full lowercase hex")
        parsed = {
            "provider": provider,
            "repository": _https_url(source["repository"], name="Git repository"),
            "commit": commit,
        }
    elif provider == "huggingface":
        _exact_fields(
            source,
            required={"provider", "repository", "revision"},
            name="component source",
        )
        repository = source["repository"]
        revision = source["revision"]
        if (
            not isinstance(repository, str)
            or HF_REPOSITORY.fullmatch(repository) is None
        ):
            raise AgentProtocolError("Hugging Face repository is invalid")
        if not isinstance(revision, str) or GIT_COMMIT.fullmatch(revision) is None:
            raise AgentProtocolError(
                "Hugging Face revision must be a full immutable revision"
            )
        parsed = {"provider": provider, "repository": repository, "revision": revision}
    elif provider in {"python-index", "signed-http-index"}:
        _exact_fields(
            source,
            required={"provider", "url", "digest"},
            name="component source",
        )
        parsed = {
            "provider": provider,
            "url": _https_url(source["url"], name="index URL"),
            "digest": _sha256(source["digest"], name="source digest", prefixed=True),
        }
    else:
        raise AgentProtocolError("component source provider is not supported")
    return _freeze(parsed)


def _parse_evidence(value: Any, *, name: str) -> Mapping[str, object]:
    evidence = _mapping(value, name=name)
    _exact_fields(evidence, required={"kind", "digest"}, name=name)
    return _freeze(
        {
            "kind": _identifier(evidence["kind"], name=f"{name} kind"),
            "digest": _sha256(evidence["digest"], name=f"{name} digest", prefixed=True),
        }
    )


def _parse_materialization(value: Any) -> Mapping[str, object]:
    materialization = _mapping(value, name="component materialization")
    _exact_fields(
        materialization,
        required={"method"},
        name="component materialization",
    )
    method = materialization["method"]
    allowed = {
        "file",
        "snapshot",
        "archive",
        "oci-content",
        "configuration",
        "native-archive",
        "wheel",
        "pylock-environment",
        "executable",
    }
    if method not in allowed:
        raise AgentProtocolError("component materialization method is not supported")
    return MappingProxyType({"method": method})


@dataclass(frozen=True)
class ComponentDescriptor:
    name: str
    kind: str
    media_type: str
    sources: tuple[Mapping[str, object], ...]
    digest: str
    size: int
    unpacked_size: int | None
    platforms: tuple[str, ...]
    materialization: Mapping[str, object]
    evidence: tuple[Mapping[str, object], ...]

    @classmethod
    def parse(cls, value: Any) -> ComponentDescriptor:
        component = _mapping(value, name="component")
        required = {
            "name",
            "kind",
            "media_type",
            "sources",
            "digest",
            "size",
            "unpacked_size",
            "platforms",
            "materialization",
            "evidence",
        }
        _exact_fields(component, required=required, name="component")
        media_type = component["media_type"]
        if not isinstance(media_type, str) or MEDIA_TYPE.fullmatch(media_type) is None:
            raise AgentProtocolError("component media_type is invalid")
        unpacked_size = component["unpacked_size"]
        if unpacked_size is not None:
            unpacked_size = _positive_integer(
                unpacked_size,
                name="component unpacked_size",
                maximum=MAX_COMPONENT_SIZE,
            )
        sources = tuple(
            _parse_source(item)
            for item in _sequence(
                component["sources"],
                name="component sources",
                minimum=1,
                maximum=MAX_SOURCES,
            )
        )
        platforms = tuple(
            _platform(item)
            for item in _sequence(
                component["platforms"],
                name="component platforms",
                minimum=1,
                maximum=16,
            )
        )
        if len(set(platforms)) != len(platforms):
            raise AgentProtocolError("component platforms contain duplicates")
        evidence = tuple(
            _parse_evidence(item, name="component evidence")
            for item in _sequence(
                component["evidence"],
                name="component evidence",
                maximum=MAX_EVIDENCE,
            )
        )
        return cls(
            name=_identifier(component["name"], name="component name"),
            kind=_identifier(component["kind"], name="component kind"),
            media_type=media_type,
            sources=sources,
            digest=_sha256(component["digest"], name="component digest", prefixed=True),
            size=_positive_integer(
                component["size"],
                name="component size",
                maximum=MAX_COMPONENT_SIZE,
            ),
            unpacked_size=unpacked_size,
            platforms=platforms,
            materialization=_parse_materialization(component["materialization"]),
            evidence=evidence,
        )


def _platform(value: Any) -> str:
    if not isinstance(value, str) or PLATFORM.fullmatch(value) is None:
        raise AgentProtocolError("component platform must be os/architecture")
    return value


def _parse_upstream_identity(value: Any) -> Mapping[str, object]:
    identity = _mapping(value, name="upstream_identity")
    provider = identity.get("provider")
    if provider == "git":
        _exact_fields(
            identity,
            required={"provider", "repository", "commit"},
            name="upstream_identity",
        )
        commit = identity["commit"]
        if not isinstance(commit, str) or GIT_COMMIT.fullmatch(commit) is None:
            raise AgentProtocolError("Git commit must be a full lowercase identity")
        result = {
            "provider": provider,
            "repository": _https_url(identity["repository"], name="Git repository"),
            "commit": commit,
        }
    elif provider == "huggingface":
        _exact_fields(
            identity,
            required={"provider", "repository", "revision"},
            name="upstream_identity",
        )
        repository = identity["repository"]
        revision = identity["revision"]
        if (
            not isinstance(repository, str)
            or HF_REPOSITORY.fullmatch(repository) is None
        ):
            raise AgentProtocolError("Hugging Face repository is invalid")
        if not isinstance(revision, str) or GIT_COMMIT.fullmatch(revision) is None:
            raise AgentProtocolError(
                "Hugging Face revision must be a full immutable revision"
            )
        result = {"provider": provider, "repository": repository, "revision": revision}
    elif provider == "oci":
        _exact_fields(
            identity,
            required={"provider", "reference"},
            name="upstream_identity",
        )
        reference = identity["reference"]
        if not isinstance(reference, str) or OCI_REFERENCE.fullmatch(reference) is None:
            raise AgentProtocolError("OCI upstream identity must use an exact digest")
        result = {"provider": provider, "reference": reference}
    elif provider == "python-index":
        _exact_fields(
            identity,
            required={"provider", "project", "version", "digest"},
            name="upstream_identity",
        )
        result = {
            "provider": provider,
            "project": _identifier(identity["project"], name="Python project"),
            "version": _bounded_text(
                identity["version"], name="Python version", maximum=128
            ),
            "digest": _sha256(
                identity["digest"], name="Python artifact digest", prefixed=True
            ),
        }
    elif provider == "signed-http-index":
        _exact_fields(
            identity,
            required={"provider", "url", "digest"},
            name="upstream_identity",
        )
        result = {
            "provider": provider,
            "url": _https_url(identity["url"], name="signed index URL"),
            "digest": _sha256(identity["digest"], name="index digest", prefixed=True),
        }
    else:
        raise AgentProtocolError("upstream_identity provider is not supported")
    return _freeze(result)


def _identifier_tuple(
    value: Any,
    *,
    name: str,
    minimum: int = 0,
    maximum: int = 32,
) -> tuple[str, ...]:
    result = tuple(
        _identifier(item, name=name)
        for item in _sequence(value, name=name, minimum=minimum, maximum=maximum)
    )
    if len(set(result)) != len(result):
        raise AgentProtocolError(f"{name} contains duplicates")
    return result


def _parse_compatibility(value: Any) -> Mapping[str, object]:
    compatibility = _mapping(value, name="compatibility")
    required = {
        "architectures",
        "operating_systems",
        "required_capabilities",
        "minimum_storage_bytes",
    }
    optional = {"minimum_driver", "minimum_cuda", "backends"}
    _exact_fields(
        compatibility,
        required=required,
        optional=optional,
        name="compatibility",
    )
    result: dict[str, object] = {
        "architectures": _identifier_tuple(
            compatibility["architectures"],
            name="compatibility architectures",
            minimum=1,
        ),
        "operating_systems": _identifier_tuple(
            compatibility["operating_systems"],
            name="compatibility operating_systems",
            minimum=1,
        ),
        "required_capabilities": _identifier_tuple(
            compatibility["required_capabilities"],
            name="compatibility required_capabilities",
        ),
        "minimum_storage_bytes": _positive_integer(
            compatibility["minimum_storage_bytes"],
            name="compatibility minimum_storage_bytes",
            maximum=MAX_COMPONENT_SIZE,
        ),
    }
    for field in ("minimum_driver", "minimum_cuda"):
        if field in compatibility:
            result[field] = _bounded_text(
                compatibility[field], name=f"compatibility {field}", maximum=64
            )
    if "backends" in compatibility:
        backends = _identifier_tuple(
            compatibility["backends"], name="compatibility backends", minimum=1
        )
        if not set(backends) <= {"oci", "python-venv", "native"}:
            raise AgentProtocolError("compatibility backend is not supported")
        result["backends"] = backends
    return _freeze(result)


def _parse_validation(value: Any, *, component_names: set[str]) -> Mapping[str, object]:
    validation = _mapping(value, name="validation record")
    _exact_fields(
        validation,
        required={"kind"},
        optional={"component", "digest", "required"},
        name="validation record",
    )
    result: dict[str, object] = {
        "kind": _identifier(validation["kind"], name="validation kind")
    }
    if "component" in validation:
        component = _identifier(validation["component"], name="validation component")
        if component not in component_names:
            raise AgentProtocolError("validation component is not declared")
        result["component"] = component
    if "digest" in validation:
        result["digest"] = _sha256(
            validation["digest"], name="validation digest", prefixed=True
        )
    if "required" in validation:
        required = validation["required"]
        if not isinstance(required, bool):
            raise AgentProtocolError("validation required must be a boolean")
        result["required"] = required
    return _freeze(result)


def _parse_resolver(value: Any) -> Mapping[str, object]:
    resolver = _mapping(value, name="resolver")
    _exact_fields(resolver, required={"name", "version"}, name="resolver")
    return MappingProxyType(
        {
            "name": _identifier(resolver["name"], name="resolver name"),
            "version": _positive_integer(
                resolver["version"], name="resolver version", maximum=2**31 - 1
            ),
        }
    )


class PackageHelperOperation(StrEnum):
    """Closed workload-only operation vocabulary accepted by the root helper."""

    PREPARE = "prepare"
    VERIFY = "verify"
    START = "start"
    HEALTH = "health"
    INFER = "infer"
    STOP = "stop"
    VERIFY_RELEASE = "verify-release"


@dataclass(frozen=True)
class PackageHelperSignature:
    algorithm: str
    key_id: str
    value: str

    def __post_init__(self) -> None:
        if (
            self.algorithm != "ed25519"
            or not isinstance(self.key_id, str)
            or SHA256.fullmatch(self.key_id) is None
            or not isinstance(self.value, str)
            or ED25519_SIGNATURE.fullmatch(self.value) is None
        ):
            raise AgentProtocolError("package helper signature is invalid")

    @classmethod
    def parse(cls, value: Any) -> PackageHelperSignature:
        document = _mapping(value, name="package helper signature")
        _exact_fields(
            document,
            required={"algorithm", "key_id", "value"},
            name="package helper signature",
        )
        return cls(document["algorithm"], document["key_id"], document["value"])

    def to_mapping(self) -> dict[str, object]:
        return {
            "algorithm": self.algorithm,
            "key_id": self.key_id,
            "value": self.value,
        }


@dataclass(frozen=True)
class PackageHelperGrantClaims:
    schema_version: int
    authority: str
    request_id: str
    node_id: str
    job_id: str
    operation_id: str
    attempt: int
    fence: str
    release_digest: str
    generation: str
    operation: PackageHelperOperation
    request_digest: str
    issued_at: int
    expires_at: int

    def __post_init__(self) -> None:
        if self.schema_version != 1 or isinstance(self.schema_version, bool):
            raise AgentProtocolError("package helper grant version is invalid")
        if self.authority != PACKAGE_HELPER_AUTHORITY:
            raise AgentProtocolError("package helper grant authority is invalid")
        _uuid4(self.request_id, name="package helper request ID")
        if not isinstance(self.node_id, str) or NODE_ID.fullmatch(self.node_id) is None:
            raise AgentProtocolError("package helper node ID is invalid")
        _uuid4(self.job_id, name="package helper job ID")
        _uuid4(self.operation_id, name="package helper operation ID")
        _positive_integer(
            self.attempt, name="package helper attempt", maximum=2**31 - 1
        )
        _uuid4(self.fence, name="package helper fence")
        _sha256(self.release_digest, name="package helper release digest", prefixed=False)
        _identifier(self.generation, name="package helper generation")
        if type(self.operation) is not PackageHelperOperation:
            raise AgentProtocolError("package helper operation is invalid")
        _sha256(self.request_digest, name="package helper request digest", prefixed=False)
        _positive_integer(
            self.issued_at, name="package helper issued_at", maximum=2**63 - 1
        )
        _positive_integer(
            self.expires_at, name="package helper expires_at", maximum=2**63 - 1
        )
        if not 1 <= self.expires_at - self.issued_at <= MAX_PACKAGE_HELPER_GRANT_SECONDS:
            raise AgentProtocolError("package helper grant expiry is invalid")

    @classmethod
    def parse(cls, value: Any) -> PackageHelperGrantClaims:
        document = _mapping(value, name="package helper grant claims")
        fields = {
            "schema_version",
            "authority",
            "request_id",
            "node_id",
            "job_id",
            "operation_id",
            "attempt",
            "fence",
            "release_digest",
            "generation",
            "operation",
            "request_digest",
            "issued_at",
            "expires_at",
        }
        _exact_fields(document, required=fields, name="package helper grant claims")
        try:
            operation = PackageHelperOperation(document["operation"])
        except (TypeError, ValueError) as error:
            raise AgentProtocolError("package helper operation is invalid") from error
        return cls(
            schema_version=document["schema_version"],
            authority=document["authority"],
            request_id=document["request_id"],
            node_id=document["node_id"],
            job_id=document["job_id"],
            operation_id=document["operation_id"],
            attempt=document["attempt"],
            fence=document["fence"],
            release_digest=document["release_digest"],
            generation=document["generation"],
            operation=operation,
            request_digest=document["request_digest"],
            issued_at=document["issued_at"],
            expires_at=document["expires_at"],
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authority": self.authority,
            "request_id": self.request_id,
            "node_id": self.node_id,
            "job_id": self.job_id,
            "operation_id": self.operation_id,
            "attempt": self.attempt,
            "fence": self.fence,
            "release_digest": self.release_digest,
            "generation": self.generation,
            "operation": self.operation.value,
            "request_digest": self.request_digest,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class SignedPackageHelperGrant:
    claims: PackageHelperGrantClaims
    signature: PackageHelperSignature

    def __post_init__(self) -> None:
        if type(self.claims) is not PackageHelperGrantClaims or type(
            self.signature
        ) is not PackageHelperSignature:
            raise AgentProtocolError("signed package helper grant is invalid")

    @classmethod
    def parse(cls, value: Any) -> SignedPackageHelperGrant:
        document = _mapping(value, name="signed package helper grant")
        _exact_fields(
            document,
            required={"claims", "signature"},
            name="signed package helper grant",
        )
        return cls(
            PackageHelperGrantClaims.parse(document["claims"]),
            PackageHelperSignature.parse(document["signature"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "claims": self.claims.to_mapping(),
            "signature": self.signature.to_mapping(),
        }


@dataclass(frozen=True)
class PackageObjectReceiptClaims:
    schema_version: int
    authority: str
    object_digest: str
    size: int
    relative_name: str

    def __post_init__(self) -> None:
        if self.schema_version != 1 or isinstance(self.schema_version, bool):
            raise AgentProtocolError("package object receipt version is invalid")
        if self.authority != PACKAGE_HELPER_AUTHORITY:
            raise AgentProtocolError("package object receipt authority is invalid")
        _sha256(self.object_digest, name="package object receipt digest", prefixed=False)
        _positive_integer(
            self.size, name="package object receipt size", maximum=2**63 - 1
        )
        if self.relative_name != f"objects/sha256/{self.object_digest}":
            raise AgentProtocolError("package object receipt relative name is invalid")

    @classmethod
    def parse(cls, value: Any) -> PackageObjectReceiptClaims:
        document = _mapping(value, name="package object receipt claims")
        _exact_fields(
            document,
            required={
                "schema_version",
                "authority",
                "object_digest",
                "size",
                "relative_name",
            },
            name="package object receipt claims",
        )
        return cls(
            document["schema_version"],
            document["authority"],
            document["object_digest"],
            document["size"],
            document["relative_name"],
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authority": self.authority,
            "object_digest": self.object_digest,
            "size": self.size,
            "relative_name": self.relative_name,
        }


@dataclass(frozen=True)
class SignedPackageObjectReceipt:
    claims: PackageObjectReceiptClaims
    signature: PackageHelperSignature

    def __post_init__(self) -> None:
        if type(self.claims) is not PackageObjectReceiptClaims or type(
            self.signature
        ) is not PackageHelperSignature:
            raise AgentProtocolError("signed package object receipt is invalid")

    @classmethod
    def parse(cls, value: Any) -> SignedPackageObjectReceipt:
        document = _mapping(value, name="signed package object receipt")
        _exact_fields(
            document,
            required={"claims", "signature"},
            name="signed package object receipt",
        )
        return cls(
            PackageObjectReceiptClaims.parse(document["claims"]),
            PackageHelperSignature.parse(document["signature"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "claims": self.claims.to_mapping(),
            "signature": self.signature.to_mapping(),
        }

    @property
    def object_digest(self) -> str:
        return self.claims.object_digest

    @property
    def size(self) -> int:
        return self.claims.size

    @property
    def relative_name(self) -> str:
        return self.claims.relative_name


def package_helper_grant_signing_bytes(claims: PackageHelperGrantClaims) -> bytes:
    if type(claims) is not PackageHelperGrantClaims:
        raise AgentProtocolError("package helper grant claims are invalid")
    return PACKAGE_HELPER_GRANT_DOMAIN + canonical_message(claims.to_mapping())


def package_object_receipt_signing_bytes(
    claims: PackageObjectReceiptClaims,
) -> bytes:
    if type(claims) is not PackageObjectReceiptClaims:
        raise AgentProtocolError("package object receipt claims are invalid")
    return PACKAGE_OBJECT_RECEIPT_DOMAIN + canonical_message(claims.to_mapping())


@dataclass(frozen=True)
class PackageReleaseLock:
    schema_version: int
    family_id: str
    upstream_version: str
    upstream_identity: Mapping[str, object]
    components: tuple[ComponentDescriptor, ...]
    dependency_digests: tuple[str, ...]
    adapter: ComponentDescriptor
    adapter_abi: int
    compatibility: Mapping[str, object]
    validation: tuple[Mapping[str, object], ...]
    provenance: tuple[Mapping[str, object], ...]
    resolver: Mapping[str, object]

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_message(self)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    @classmethod
    def parse(cls, value: Any) -> PackageReleaseLock:
        document = _load_document(value)
        required = {
            "schema_version",
            "family_id",
            "upstream_version",
            "upstream_identity",
            "components",
            "dependency_digests",
            "adapter",
            "adapter_abi",
            "compatibility",
            "validation",
            "provenance",
            "resolver",
        }
        _exact_fields(document, required=required, name="workload release lock")
        if document["schema_version"] != 1 or isinstance(
            document["schema_version"], bool
        ):
            raise AgentProtocolError("unsupported workload release lock schema_version")
        components = tuple(
            ComponentDescriptor.parse(item)
            for item in _sequence(
                document["components"],
                name="components",
                maximum=MAX_AGGREGATE_COMPONENTS - 1,
            )
        )
        component_names = [item.name for item in components]
        if len(set(component_names)) != len(component_names):
            raise AgentProtocolError("duplicate component name")
        adapter = ComponentDescriptor.parse(document["adapter"])
        if adapter.kind != "adapter":
            raise AgentProtocolError("adapter component kind must be adapter")
        if adapter.name in component_names:
            raise AgentProtocolError("duplicate component name")
        dependencies = tuple(
            _sha256(item, name="dependency digest", prefixed=False)
            for item in _sequence(
                document["dependency_digests"],
                name="dependency_digests",
                maximum=MAX_AGGREGATE_COMPONENTS,
            )
        )
        if len(set(dependencies)) != len(dependencies):
            raise AgentProtocolError("duplicate dependency digest")
        all_component_names = {*component_names, adapter.name}
        validation = tuple(
            _parse_validation(item, component_names=all_component_names)
            for item in _sequence(
                document["validation"],
                name="validation",
                maximum=64,
            )
        )
        provenance = tuple(
            _parse_evidence(item, name="provenance record")
            for item in _sequence(
                document["provenance"],
                name="provenance",
                maximum=64,
            )
        )
        lock = cls(
            schema_version=1,
            family_id=_identifier(document["family_id"], name="family_id"),
            upstream_version=_bounded_text(
                document["upstream_version"], name="upstream_version", maximum=128
            ),
            upstream_identity=_parse_upstream_identity(document["upstream_identity"]),
            components=components,
            dependency_digests=dependencies,
            adapter=adapter,
            adapter_abi=_positive_integer(
                document["adapter_abi"], name="adapter_abi", maximum=255
            ),
            compatibility=_parse_compatibility(document["compatibility"]),
            validation=validation,
            provenance=provenance,
            resolver=_parse_resolver(document["resolver"]),
        )
        if len(lock.canonical_bytes) > MAX_RELEASE_LOCK_BYTES:
            raise AgentProtocolError("workload release lock is too large")
        return lock


@dataclass(frozen=True)
class PackageReleaseGraph:
    root_digest: str
    releases: tuple[PackageReleaseLock, ...]

    @property
    def component_count(self) -> int:
        return sum(len(release.components) + 1 for release in self.releases)

    @property
    def total_size(self) -> int:
        return sum(
            component.size
            for release in self.releases
            for component in (*release.components, release.adapter)
        )

    @classmethod
    def resolve(
        cls,
        root_digest: str,
        releases: Mapping[str, PackageReleaseLock],
    ) -> PackageReleaseGraph:
        root_digest = _sha256(root_digest, name="root digest", prefixed=False)
        if not isinstance(releases, Mapping):
            raise AgentProtocolError("releases must be a digest mapping")
        visiting: set[str] = set()
        resolved: set[str] = set()
        ordered: list[PackageReleaseLock] = []
        component_count = 0

        def visit(digest: str, depth: int) -> None:
            nonlocal component_count
            if digest in visiting:
                raise AgentProtocolError("package dependency cycle detected")
            if digest in resolved:
                return
            if depth > MAX_DEPENDENCY_DEPTH:
                raise AgentProtocolError("package dependency depth exceeds 8")
            release = releases.get(digest)
            if not isinstance(release, PackageReleaseLock):
                raise AgentProtocolError(f"package dependency is missing: {digest}")
            visiting.add(digest)
            ordered.append(release)
            component_count += len(release.components) + 1
            if component_count > MAX_AGGREGATE_COMPONENTS:
                raise AgentProtocolError("package graph component count exceeds 256")
            for dependency in release.dependency_digests:
                visit(dependency, depth + 1)
            if release.digest != digest:
                raise AgentProtocolError("package release digest mismatch")
            visiting.remove(digest)
            resolved.add(digest)

        visit(root_digest, 0)
        return cls(root_digest=root_digest, releases=tuple(ordered))


__all__ = [
    "MAX_PACKAGE_HELPER_GRANT_SECONDS",
    "PACKAGE_HELPER_AUTHORITY",
    "ComponentDescriptor",
    "PackageHelperGrantClaims",
    "PackageHelperOperation",
    "PackageHelperSignature",
    "PackageObjectReceiptClaims",
    "PackageReleaseGraph",
    "PackageReleaseLock",
    "SignedPackageHelperGrant",
    "SignedPackageObjectReceipt",
    "package_helper_grant_signing_bytes",
    "package_object_receipt_signing_bytes",
]
