"""Strict, content-addressed DGX-Forge platform release contracts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate

_MAX_MANIFEST_BYTES = 1024 * 1024
_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_DIGEST = re.compile(r"sha256:([0-9a-f]{64})\Z")


class PlatformReleaseError(ValueError):
    """A platform release manifest is invalid or unsafe."""


@dataclass(frozen=True)
class ProtocolRange:
    minimum: int
    maximum: int

    def contains(self, version: int) -> bool:
        return self.minimum <= version <= self.maximum


@dataclass(frozen=True)
class Artifact:
    name: str
    reference: str
    sha256: str
    size: int
    sbom_sha256: str
    provenance_sha256: str


@dataclass(frozen=True)
class ArchitectureArtifact:
    architecture: str
    artifact: Artifact
    protocol: ProtocolRange | None = None


@dataclass(frozen=True)
class ControlRelease:
    config_version: int
    protocol: ProtocolRange
    api_image: Artifact
    worker_image: Artifact
    assets: tuple[Artifact, ...]


@dataclass(frozen=True)
class DatabaseRelease:
    expand_revision: str
    contract_revision: str | None
    predecessor_compatible: bool


@dataclass(frozen=True)
class PlatformIdentity:
    platform_version: str
    build_digest: str
    architecture: str
    control_api_protocol: int
    agent_protocol: int

    def __post_init__(self) -> None:
        _semantic_version(self.platform_version)
        _prefixed_digest(self.build_digest, "build digest")
        if self.architecture not in {"linux-arm64", "linux-x86_64"}:
            raise PlatformReleaseError("platform architecture is invalid")
        for value in (self.control_api_protocol, self.agent_protocol):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
                raise PlatformReleaseError("platform protocol version is invalid")


@dataclass(frozen=True)
class CompatibilityReport:
    compatible: bool
    update_recommended: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PlatformRelease:
    platform_version: str
    build_digest: str
    control: ControlRelease
    database: DatabaseRelease
    agents: tuple[ArchitectureArtifact, ...]
    supervisors: tuple[ArchitectureArtifact, ...]
    tooling: tuple[ArchitectureArtifact, ...]
    compatible_predecessor_builds: tuple[str, ...]
    digest: str

    @classmethod
    def load(cls, path: Path) -> PlatformRelease:
        try:
            raw = Path(path).read_bytes()
        except OSError as error:
            raise PlatformReleaseError("platform release manifest is unreadable") from error
        if not raw or len(raw) > _MAX_MANIFEST_BYTES:
            raise PlatformReleaseError("platform release manifest size is invalid")
        try:
            document = json.loads(raw, object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PlatformReleaseError("platform release manifest is not valid JSON") from error
        if not isinstance(document, dict):
            raise PlatformReleaseError("platform release manifest must be an object")
        try:
            validate(instance=document, schema=_schema())
        except ValidationError as error:
            location = ".".join(str(part) for part in error.absolute_path)
            prefix = f"{location}: " if location else ""
            raise PlatformReleaseError(f"{prefix}{error.message}") from error
        return cls._parse(document)

    @classmethod
    def _parse(cls, document: dict[str, Any]) -> PlatformRelease:
        control_document = document["control"]
        database_document = document["database"]
        agents = _architecture_artifacts(document["agents"], require_protocol=True)
        supervisors = _architecture_artifacts(document["supervisors"], require_protocol=False)
        tooling = _architecture_artifacts(document["tooling"], require_protocol=False)
        control_protocol = _protocol(control_document["protocol"])
        if database_document["contract_revision"] is not None and not database_document["predecessor_compatible"]:
            raise PlatformReleaseError(
                "contract migration is not predecessor compatible"
            )
        predecessors = tuple(document["rollback"]["compatible_predecessor_builds"])
        if not predecessors:
            raise PlatformReleaseError("at least one recovery predecessor is required")
        canonical = (
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        images = control_document["images"]
        return cls(
            platform_version=_semantic_version(document["platform_version"]),
            build_digest=_prefixed_digest(document["build_digest"], "build digest"),
            control=ControlRelease(
                config_version=control_document["config_version"],
                protocol=control_protocol,
                api_image=_artifact(images["api"]),
                worker_image=_artifact(images["worker"]),
                assets=tuple(_artifact(item) for item in control_document["assets"]),
            ),
            database=DatabaseRelease(
                expand_revision=database_document["expand_revision"],
                contract_revision=database_document["contract_revision"],
                predecessor_compatible=database_document["predecessor_compatible"],
            ),
            agents=agents,
            supervisors=supervisors,
            tooling=tooling,
            compatible_predecessor_builds=predecessors,
            digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
        )

    def agent_for(self, architecture: str) -> ArchitectureArtifact:
        for artifact in self.agents:
            if artifact.architecture == architecture:
                return artifact
        raise PlatformReleaseError("agent architecture is not published")

    def compatibility(self, current: PlatformIdentity) -> CompatibilityReport:
        reasons: list[str] = []
        agent = next(
            (item for item in self.agents if item.architecture == current.architecture),
            None,
        )
        if agent is None:
            reasons.append("architecture-not-published")
        elif agent.protocol is not None and not agent.protocol.contains(current.agent_protocol):
            reasons.append("agent-protocol-incompatible")
        if not self.control.protocol.contains(current.control_api_protocol):
            reasons.append("control-protocol-incompatible")
        if (
            current.build_digest != self.build_digest
            and current.build_digest not in self.compatible_predecessor_builds
        ):
            reasons.append("predecessor-not-recovery-compatible")
        if _semantic_tuple(current.platform_version) > _semantic_tuple(self.platform_version):
            reasons.append("platform-downgrade-forbidden")
        return CompatibilityReport(
            compatible=not reasons,
            update_recommended=current.build_digest != self.build_digest,
            reasons=tuple(reasons),
        )


def _schema() -> dict[str, Any]:
    try:
        schema = resources.files("spark_profiles").joinpath(
            "schemas", "platform-update-manifest.schema.json"
        )
        with schema.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError) as error:
        raise RuntimeError("platform update schema is unavailable") from error
    if not isinstance(value, dict):
        raise TypeError("platform update schema is invalid")
    return value


def _artifact(document: dict[str, Any]) -> Artifact:
    match = _DIGEST.search(document["reference"])
    if match is None or match.group(1) != document["sha256"]:
        raise PlatformReleaseError("artifact reference digest does not match sha256")
    return Artifact(
        name=document["name"],
        reference=document["reference"],
        sha256=document["sha256"],
        size=document["size"],
        sbom_sha256=document["sbom_sha256"],
        provenance_sha256=document["provenance_sha256"],
    )


def _architecture_artifacts(
    documents: list[dict[str, Any]], *, require_protocol: bool
) -> tuple[ArchitectureArtifact, ...]:
    seen: set[str] = set()
    result: list[ArchitectureArtifact] = []
    for document in documents:
        architecture = document["architecture"]
        if architecture in seen:
            raise PlatformReleaseError("architecture entries overlap")
        seen.add(architecture)
        result.append(
            ArchitectureArtifact(
                architecture=architecture,
                artifact=_artifact(document["artifact"]),
                protocol=_protocol(document["protocol"]) if require_protocol else None,
            )
        )
    return tuple(result)


def _protocol(document: dict[str, int]) -> ProtocolRange:
    value = ProtocolRange(document["minimum"], document["maximum"])
    if value.minimum > value.maximum:
        raise PlatformReleaseError("protocol range is invalid")
    return value


def _semantic_version(value: str) -> str:
    if not isinstance(value, str) or _SEMVER.fullmatch(value) is None:
        raise PlatformReleaseError("semantic version is invalid")
    return value


def _semantic_tuple(value: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(value)
    if match is None:
        raise PlatformReleaseError("semantic version is invalid")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _prefixed_digest(value: str, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise PlatformReleaseError(f"{label} is invalid")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise PlatformReleaseError("platform release contains duplicate fields")
        document[key] = value
    return document
