"""Bounded, non-executing parser for untrusted SparkRun YAML profiles."""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from typing import Any

import yaml
from yaml.events import AliasEvent
from yaml.nodes import MappingNode

_MAX_INPUT = 256 * 1024
_MAX_NODES = 4096
_MAX_ALIASES = 16
_MAX_DEPTH = 32
_MAX_STRING = 64 * 1024
_KNOWN_FIELDS = frozenset(
    {
        "recipe_version", "model", "model_revision", "runtime", "container",
        "min_nodes", "max_nodes", "metadata", "defaults", "env", "command",
        "mods", "tuning", "benchmark",
    }
)
_SENSITIVE = re.compile(
    r"(?:^|_)(?:authorization|credential|password|secret|token|private_key|certificate)(?:$|_)",
    re.IGNORECASE,
)


class SparkRunParseError(ValueError):
    pass


class _BoundedSafeLoader(yaml.SafeLoader):
    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self.node_count = 0
        self.alias_count = 0
        self.depth = 0

    def compose_node(self, parent: Any, index: Any):  # type: ignore[no-untyped-def]
        if self.check_event(AliasEvent):
            self.alias_count += 1
            if self.alias_count > _MAX_ALIASES:
                raise SparkRunParseError("SparkRun YAML has too many aliases")
        self.node_count += 1
        if self.node_count > _MAX_NODES:
            raise SparkRunParseError("SparkRun YAML has too many nodes")
        self.depth += 1
        if self.depth > _MAX_DEPTH:
            raise SparkRunParseError("SparkRun YAML is nested too deeply")
        try:
            return super().compose_node(parent, index)
        finally:
            self.depth -= 1

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[object] = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=False)
            try:
                duplicate = key in seen
                seen.add(key)
            except TypeError as error:
                raise SparkRunParseError("SparkRun YAML mapping key is invalid") from error
            if duplicate:
                raise SparkRunParseError("SparkRun YAML contains a duplicate key")
        return super().construct_mapping(node, deep=deep)


@dataclass(frozen=True, slots=True)
class UnknownField:
    path: str
    value_type: str


@dataclass(frozen=True, slots=True)
class SparkRunCommand:
    raw: str


@dataclass(frozen=True, slots=True)
class SparkRunDefaults:
    values: dict[str, object]


@dataclass(frozen=True, slots=True)
class SparkRunMetadata:
    title: str | None
    description: str | None
    tags: tuple[str, ...]
    values: dict[str, object]


@dataclass(frozen=True, slots=True)
class SparkRunSource:
    recipe_version: int | str | None
    model: str
    model_revision: str | None
    runtime: str
    container: str | None
    min_nodes: int | None
    max_nodes: int | None
    metadata: SparkRunMetadata
    defaults: SparkRunDefaults
    environment: dict[str, object]
    command: SparkRunCommand
    mods: tuple[object, ...]
    tuning: dict[str, object]
    benchmark: dict[str, object]
    unknown_fields: tuple[UnknownField, ...]
    source_sha256: str
    document: dict[str, object]

    def leaf_paths(self) -> tuple[str, ...]:
        paths: list[str] = []

        def visit(value: object, path: str, ancestors: set[int]) -> None:
            if isinstance(value, dict):
                identity = id(value)
                if identity in ancestors:
                    raise SparkRunParseError("recursive YAML aliases are forbidden")
                next_ancestors = ancestors | {identity}
                if not value:
                    paths.append(path)
                for key in sorted(value):
                    escaped = key.replace("~", "~0").replace("/", "~1")
                    visit(value[key], f"{path}/{escaped}", next_ancestors)
            elif isinstance(value, list):
                identity = id(value)
                if identity in ancestors:
                    raise SparkRunParseError("recursive YAML aliases are forbidden")
                next_ancestors = ancestors | {identity}
                if not value:
                    paths.append(path)
                for index, child in enumerate(value):
                    visit(child, f"{path}/{index}", next_ancestors)
            else:
                paths.append(path)

        visit(self.document, "", set())
        return tuple(paths)


def parse_sparkrun_yaml(raw: bytes) -> SparkRunSource:
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_INPUT:
        raise SparkRunParseError("SparkRun YAML size is invalid")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SparkRunParseError("SparkRun YAML must be UTF-8") from error
    if "\x00" in text:
        raise SparkRunParseError("SparkRun YAML contains a forbidden character")
    try:
        documents = list(yaml.load_all(text, Loader=_BoundedSafeLoader))
    except SparkRunParseError:
        raise
    except yaml.YAMLError as error:
        raise SparkRunParseError("SparkRun YAML is invalid or unsafe") from error
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise SparkRunParseError("SparkRun YAML must contain exactly one mapping")
    document = _validate_json_value(documents[0], "$", set())
    assert isinstance(document, dict)
    model = _required_string(document, "model")
    runtime = _required_string(document, "runtime")
    command_value = document.get("command", "")
    if isinstance(command_value, list) and all(isinstance(item, str) for item in command_value):
        command_raw = " ".join(command_value)
    elif isinstance(command_value, str):
        command_raw = command_value
    else:
        raise SparkRunParseError("SparkRun command must be text or a text list")
    env = _mapping(document.get("env", {}), "env")
    for key, value in env.items():
        if _SENSITIVE.search(key) or (
            isinstance(value, str)
            and (value.lower().startswith("bearer ") or "private key" in value.lower())
        ):
            raise SparkRunParseError("SparkRun environment contains a secret-shaped field")
    metadata_values = _mapping(document.get("metadata", {}), "metadata")
    tags = metadata_values.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(item, str) for item in tags):
        raise SparkRunParseError("SparkRun metadata tags are invalid")
    unknown = tuple(
        UnknownField(f"/{key.replace('~', '~0').replace('/', '~1')}", _value_type(value))
        for key, value in sorted(document.items())
        if key not in _KNOWN_FIELDS
    )
    return SparkRunSource(
        recipe_version=document.get("recipe_version") if isinstance(document.get("recipe_version"), (int, str)) else None,
        model=model,
        model_revision=_optional_string(document, "model_revision"),
        runtime=runtime,
        container=_optional_string(document, "container"),
        min_nodes=_optional_positive_integer(document, "min_nodes"),
        max_nodes=_optional_positive_integer(document, "max_nodes"),
        metadata=SparkRunMetadata(
            title=_optional_string(metadata_values, "title"),
            description=_optional_string(metadata_values, "description"),
            tags=tuple(tags),
            values=copy.deepcopy(metadata_values),
        ),
        defaults=SparkRunDefaults(copy.deepcopy(_mapping(document.get("defaults", {}), "defaults"))),
        environment=copy.deepcopy(env),
        command=SparkRunCommand(command_raw),
        mods=tuple(copy.deepcopy(_sequence(document.get("mods", []), "mods"))),
        tuning=copy.deepcopy(_mapping(document.get("tuning", {}), "tuning")),
        benchmark=copy.deepcopy(_mapping(document.get("benchmark", {}), "benchmark")),
        unknown_fields=unknown,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        document=copy.deepcopy(document),
    )


def _validate_json_value(value: object, path: str, ancestors: set[int]) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value.encode()) > _MAX_STRING:
            raise SparkRunParseError(f"SparkRun scalar is too large at {path}")
        return value
    if isinstance(value, dict):
        identity = id(value)
        if identity in ancestors:
            raise SparkRunParseError("recursive YAML aliases are forbidden")
        result: dict[str, object] = {}
        for key, child in value.items():
            if not isinstance(key, str) or len(key.encode()) > _MAX_STRING:
                raise SparkRunParseError(f"SparkRun mapping key is invalid at {path}")
            result[key] = _validate_json_value(child, f"{path}.{key}", ancestors | {identity})
        return result
    if isinstance(value, list):
        identity = id(value)
        if identity in ancestors:
            raise SparkRunParseError("recursive YAML aliases are forbidden")
        return [
            _validate_json_value(child, f"{path}[{index}]", ancestors | {identity})
            for index, child in enumerate(value)
        ]
    raise SparkRunParseError(f"SparkRun value type is invalid at {path}")


def _required_string(document: dict[str, object], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise SparkRunParseError(f"SparkRun {key} is invalid")
    return value


def _optional_string(document: dict[str, object], key: str) -> str | None:
    value = document.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise SparkRunParseError(f"SparkRun {key} is invalid")
    return value


def _optional_positive_integer(document: dict[str, object], key: str) -> int | None:
    value = document.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 16:
        raise SparkRunParseError(f"SparkRun {key} is invalid")
    return value


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SparkRunParseError(f"SparkRun {field} must be a mapping")
    return value


def _sequence(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise SparkRunParseError(f"SparkRun {field} must be a list")
    return value


def _value_type(value: object) -> str:
    if isinstance(value, dict):
        return "mapping"
    if isinstance(value, list):
        return "sequence"
    return type(value).__name__
