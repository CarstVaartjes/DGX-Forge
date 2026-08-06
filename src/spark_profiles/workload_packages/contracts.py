"""Strict immutable contracts for Git-authored workload repository state."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, ValidationError

_IDENTIFIER = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_OCI_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_OCI_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_OCI_LOCATOR = re.compile(
    rf"{_OCI_HOST_LABEL}(?:\.{_OCI_HOST_LABEL})*"
    rf"(?::(?P<port>[1-9][0-9]{{0,4}}))?/"
    rf"{_OCI_COMPONENT}(?:/{_OCI_COMPONENT})*\Z"
)
_HF_LOCATOR = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z"
)
_PYPI_LOCATOR = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*\Z")
_UNSAFE_ARGUMENT = re.compile(
    r"(?i)(?:https?://|file://|secret://|(?:token|password|secret|api[_-]?key)\s*=|"
    r"-----BEGIN|(?:^|=)/|(?:^|/)\.\.(?:/|$))"
)
_MAX_DOCUMENT_BYTES = 1024 * 1024


class WorkloadPackageError(ValueError):
    """Git-authored workload state is invalid or crosses an authority boundary."""


@dataclass(frozen=True)
class PromotionPolicy:
    mode: Literal["manual", "automatic"]
    automation_identity: str | None = None
    failure_budget: int | None = None
    canary: Mapping[str, int] | None = None

    @classmethod
    def load(cls, document: Mapping[str, object] | None) -> PromotionPolicy:
        value: Mapping[str, object] = (
            {"mode": "manual"} if document is None else document
        )
        if not isinstance(value, Mapping):
            raise WorkloadPackageError("promotion policy must be an object")
        fields = set(value)
        mode = value.get("mode")
        if mode == "manual":
            if fields != {"mode"}:
                raise WorkloadPackageError("manual promotion has unexpected fields")
            return cls(mode="manual")
        if mode != "automatic" or fields != {
            "mode",
            "automation_identity",
            "failure_budget",
            "canary",
        }:
            raise WorkloadPackageError("automatic promotion policy is incomplete")
        identity = value.get("automation_identity")
        budget = value.get("failure_budget")
        canary = value.get("canary")
        if (
            not isinstance(identity, str)
            or re.fullmatch(
                r"automation://[a-z][a-z0-9]*(?:[._/-][a-z0-9]+)*", identity
            )
            is None
            or isinstance(budget, bool)
            or not isinstance(budget, int)
            or not 1 <= budget <= 100
            or not isinstance(canary, Mapping)
            or set(canary) != {"node_count", "minimum_successes"}
        ):
            raise WorkloadPackageError("automatic promotion policy is invalid")
        node_count = canary.get("node_count")
        minimum_successes = canary.get("minimum_successes")
        if (
            isinstance(node_count, bool)
            or not isinstance(node_count, int)
            or not 1 <= node_count <= 256
            or isinstance(minimum_successes, bool)
            or not isinstance(minimum_successes, int)
            or not 1 <= minimum_successes <= node_count
        ):
            raise WorkloadPackageError("promotion canary is invalid")
        return cls(
            mode="automatic",
            automation_identity=identity,
            failure_budget=budget,
            canary=MappingProxyType(
                {"node_count": node_count, "minimum_successes": minimum_successes}
            ),
        )


@dataclass(frozen=True)
class PackageFamily:
    schema_version: int
    family_id: str
    source: Mapping[str, object]
    versions: Mapping[str, object]
    discovery: Mapping[str, object]
    resolution: Mapping[str, object]
    policy: Mapping[str, object]
    compatibility: Mapping[str, object]
    execution: Mapping[str, object]
    validation: tuple[Mapping[str, object], ...]
    promotion: PromotionPolicy
    retention: Mapping[str, object]
    canonical_bytes: bytes

    @classmethod
    def load(cls, document: Mapping[str, object]) -> PackageFamily:
        value = _json_document(document, "package family")
        value.setdefault("promotion", {"mode": "manual"})
        _validate(value, "package-family.schema.json", "package family")
        _validate_source(value["source"])
        _validate_recipe(value["discovery"], value["resolution"])
        promotion = PromotionPolicy.load(value["promotion"])
        retention = value["retention"]
        if retention["rollback_count"] >= retention["release_count"]:
            raise WorkloadPackageError(
                "package family rollback retention must be below release retention"
            )
        canonical = _canonical(value)
        return cls(
            schema_version=value["schema_version"],
            family_id=value["family_id"],
            source=_freeze(value["source"]),
            versions=_freeze(value["versions"]),
            discovery=_freeze(value["discovery"]),
            resolution=_freeze(value["resolution"]),
            policy=_freeze(value["policy"]),
            compatibility=_freeze(value["compatibility"]),
            execution=_freeze(value["execution"]),
            validation=tuple(_freeze(item) for item in value["validation"]),
            promotion=promotion,
            retention=_freeze(retention),
            canonical_bytes=canonical,
        )

    @property
    def repository_path(self) -> PurePosixPath:
        return PurePosixPath("config/package-families") / f"{self.family_id}.toml"


@dataclass(frozen=True)
class WorkloadDeployment:
    schema_version: int
    deployment_id: str
    family_id: str
    release_digest: str
    selector: Mapping[str, object]
    secrets: Mapping[str, str]
    ports: Mapping[str, int]
    arguments: tuple[str, ...]
    routing: Mapping[str, object]
    resources: Mapping[str, int]
    canonical_bytes: bytes

    @classmethod
    def load(cls, document: Mapping[str, object]) -> WorkloadDeployment:
        value = _json_document(document, "workload deployment")
        _validate(value, "workload-deployment.schema.json", "workload deployment")
        for argument in value["arguments"]:
            if _UNSAFE_ARGUMENT.search(argument):
                raise WorkloadPackageError(
                    "workload deployment argument contains payload or secret material"
                )
        if value["routing"]["port"] not in value["ports"]:
            raise WorkloadPackageError(
                "workload deployment route references an unknown port"
            )
        return cls(
            schema_version=value["schema_version"],
            deployment_id=value["deployment_id"],
            family_id=value["family_id"],
            release_digest=value["release_digest"],
            selector=_freeze(value["selector"]),
            secrets=_freeze(value["secrets"]),
            ports=_freeze(value["ports"]),
            arguments=tuple(value["arguments"]),
            routing=_freeze(value["routing"]),
            resources=_freeze(value["resources"]),
            canonical_bytes=_canonical(value),
        )

    @property
    def repository_path(self) -> PurePosixPath:
        return (
            PurePosixPath("config/workload-deployments") / f"{self.deployment_id}.toml"
        )


@dataclass(frozen=True)
class ReleaseIndexEntry:
    family_id: str
    release_digest: str
    upstream_version: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.family_id, str)
            or _IDENTIFIER.fullmatch(self.family_id) is None
            or not isinstance(self.release_digest, str)
            or _DIGEST.fullmatch(self.release_digest) is None
            or (
                self.upstream_version is not None
                and (
                    not isinstance(self.upstream_version, str)
                    or not 1 <= len(self.upstream_version) <= 256
                    or any(
                        ord(character) < 32 or ord(character) == 127
                        for character in self.upstream_version
                    )
                )
            )
        ):
            raise ValueError("workload release index entry is invalid")

    @property
    def repository_path(self) -> PurePosixPath:
        return (
            PurePosixPath("manifests/workload-releases")
            / self.family_id
            / f"{self.release_digest}.json"
        )


def validate_deployment(
    deployment: WorkloadDeployment,
    releases: Mapping[str, ReleaseIndexEntry],
) -> ReleaseIndexEntry:
    """Resolve a deployment only through an exact promoted release index entry."""
    if not isinstance(deployment, WorkloadDeployment) or not isinstance(
        releases, Mapping
    ):
        raise WorkloadPackageError("deployment release index is invalid")
    release = releases.get(deployment.release_digest)
    if release is None:
        raise WorkloadPackageError("deployment release is not promoted")
    if (
        not isinstance(release, ReleaseIndexEntry)
        or release.release_digest != deployment.release_digest
    ):
        raise WorkloadPackageError("deployment release index identity is invalid")
    if release.family_id != deployment.family_id:
        raise WorkloadPackageError("deployment release family does not match")
    return release


def _json_document(document: Mapping[str, object], label: str) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise WorkloadPackageError(f"{label} must be an object")
    try:
        raw = json.dumps(
            dict(document),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise WorkloadPackageError(
            f"{label} must contain JSON-compatible values"
        ) from error
    if not raw or len(raw) > _MAX_DOCUMENT_BYTES:
        raise WorkloadPackageError(f"{label} size is invalid")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise WorkloadPackageError(f"{label} must be an object")
    return value


def _canonical(document: Mapping[str, object]) -> bytes:
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _schema(name: str) -> dict[str, Any]:
    try:
        raw = resources.files("spark_profiles.schemas").joinpath(name).read_text()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise WorkloadPackageError("packaged workload schema is unavailable") from error
    if not isinstance(value, dict):
        raise WorkloadPackageError("packaged workload schema is invalid")
    return value


def _validate(document: Mapping[str, object], schema: str, label: str) -> None:
    try:
        Draft202012Validator(_schema(schema)).validate(document)
    except ValidationError as error:
        location = ".".join(str(part) for part in error.absolute_path)
        prefix = f"{location}: " if location else ""
        raise WorkloadPackageError(
            f"{label} is invalid: {prefix}{error.message}"
        ) from error


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def _validate_source(source: Mapping[str, object]) -> None:
    provider = source["provider"]
    locator = source["locator"]
    valid = False
    if provider in {"git", "signed-http-index"}:
        parsed = urlsplit(locator)
        valid = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and parsed.path not in {"", "/"}
        )
        if provider == "signed-http-index":
            valid = valid and bool(parsed.path) and not parsed.path.endswith("/")
    elif provider == "oci":
        match = _OCI_LOCATOR.fullmatch(locator)
        valid = (
            match is not None
            and (match.group("port") is None or int(match.group("port")) <= 65535)
            and "@" not in locator
            and "://" not in locator
        )
    elif provider == "huggingface":
        valid = _HF_LOCATOR.fullmatch(locator) is not None
    elif provider == "python-index":
        valid = _PYPI_LOCATOR.fullmatch(locator) is not None
    if not valid:
        raise WorkloadPackageError("package family source locator is invalid")


def _validate_recipe(
    discovery: Mapping[str, object], resolution: Mapping[str, object]
) -> None:
    bindings = discovery["bindings"]
    components = resolution["components"]
    dependencies = resolution["dependencies"]
    targets = [binding["target"] for binding in bindings]
    component_names = [component["name"] for component in components]
    dependency_families = [dependency["family_id"] for dependency in dependencies]
    if len(targets) != len(set(targets)):
        raise WorkloadPackageError("package family has duplicate discovery target")
    if len(component_names) != len(set(component_names)):
        raise WorkloadPackageError("package family has duplicate component template")
    if len(dependency_families) != len(set(dependency_families)):
        raise WorkloadPackageError("package family has duplicate dependency template")
