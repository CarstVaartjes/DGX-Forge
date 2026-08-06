"""A tiny allowlist grammar for runtime commands; deliberately not a shell."""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from ..sparkrun_source import SparkRunSource

_PLACEHOLDER = re.compile(r"\{([a-zA-Z][a-zA-Z0-9_]*)\}")
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_./:+@%=-]{1,2048}$")


class RuntimeCompileError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FlagSpec:
    canonical: str
    takes_value: bool = True
    emit: bool = True
    validate: Callable[[str], bool] = lambda _value: True


@dataclass(frozen=True, slots=True)
class RuntimeProjection:
    family: str
    arguments: tuple[str, ...]
    environment: dict[str, str]
    endpoint: dict[str, object]
    transformed_paths: tuple[str, ...]
    security_capabilities: tuple[str, ...] = ()
    topology_requirement: str = "single"

    def recipe_arguments(self) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        index = 0
        while index < len(self.arguments):
            flag = self.arguments[index]
            name = flag.removeprefix("--").replace("-", "_")
            if index + 1 < len(self.arguments) and not self.arguments[index + 1].startswith("--"):
                result.append({"name": name, "value": self.arguments[index + 1]})
                index += 2
            else:
                result.append({"name": name, "value": True})
                index += 1
        return result


def tokens(source: SparkRunSource) -> list[str]:
    raw = source.command.raw.replace("\\\n", " ")
    if any(character in raw for character in (";", "|", "&", ">", "<", "`", "$", "\n", "\r")):
        raise RuntimeCompileError("runtime command contains forbidden shell syntax")
    values: dict[str, object] = {"model": source.model, **source.defaults.values}

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values or isinstance(values[name], (dict, list)):
            raise RuntimeCompileError(f"runtime placeholder is undeclared: {name}")
        rendered = str(values[name]).lower() if isinstance(values[name], bool) else str(values[name])
        if _SAFE_VALUE.fullmatch(rendered) is None:
            raise RuntimeCompileError(f"runtime placeholder value is unsafe: {name}")
        return rendered

    expanded = _PLACEHOLDER.sub(substitute, raw)
    if "{" in expanded or "}" in expanded:
        raise RuntimeCompileError("runtime command contains an invalid placeholder")
    try:
        result = shlex.split(expanded, posix=True)
    except ValueError as error:
        raise RuntimeCompileError("runtime command quoting is invalid") from error
    if not result or len(result) > 256 or any(len(item) > 2048 for item in result):
        raise RuntimeCompileError("runtime command size is invalid")
    return result


def options(
    values: Sequence[str], specifications: Mapping[str, FlagSpec]
) -> tuple[tuple[str, ...], dict[str, str | bool]]:
    emitted: list[str] = []
    parsed: dict[str, str | bool] = {}
    index = 0
    while index < len(values):
        raw_flag = values[index]
        inline: str | None = None
        if "=" in raw_flag:
            raw_flag, inline = raw_flag.split("=", 1)
        spec = specifications.get(raw_flag)
        if spec is None:
            raise RuntimeCompileError(f"runtime flag is not allowlisted: {raw_flag}")
        if spec.canonical in parsed:
            raise RuntimeCompileError(f"runtime flag is repeated: {spec.canonical}")
        if spec.takes_value:
            if inline is None:
                index += 1
                if index >= len(values) or values[index].startswith("-"):
                    raise RuntimeCompileError(f"runtime flag requires a value: {raw_flag}")
                value = values[index]
            else:
                value = inline
            if not spec.validate(value) or _SAFE_VALUE.fullmatch(value) is None:
                raise RuntimeCompileError(f"runtime flag value is invalid: {raw_flag}")
            parsed[spec.canonical] = value
            if spec.emit:
                emitted.extend((spec.canonical, value))
        else:
            if inline is not None:
                raise RuntimeCompileError(f"runtime presence flag cannot have a value: {raw_flag}")
            parsed[spec.canonical] = True
            if spec.emit:
                emitted.append(spec.canonical)
        index += 1
    return tuple(emitted), parsed


def environment(source: SparkRunSource, allowlist: frozenset[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in source.environment.items():
        if key not in allowlist or not isinstance(value, (str, int, bool)):
            raise RuntimeCompileError(f"runtime environment field is not allowlisted: {key}")
        rendered = str(value).lower() if isinstance(value, bool) else str(value)
        if _SAFE_VALUE.fullmatch(rendered) is None:
            raise RuntimeCompileError(f"runtime environment value is invalid: {key}")
        result[key] = rendered
    return result


def integer(minimum: int, maximum: int) -> Callable[[str], bool]:
    def validate(value: str) -> bool:
        try:
            parsed = int(value)
        except ValueError:
            return False
        return str(parsed) == value and minimum <= parsed <= maximum
    return validate


def decimal(minimum: float, maximum: float) -> Callable[[str], bool]:
    def validate(value: str) -> bool:
        try:
            parsed = float(value)
        except ValueError:
            return False
        return minimum <= parsed <= maximum and parsed == parsed
    return validate


def one_of(*accepted: str) -> Callable[[str], bool]:
    allowed = frozenset(accepted)
    return lambda value: value in allowed
