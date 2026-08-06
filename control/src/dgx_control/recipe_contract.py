from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


class RecipeContractError(ValueError):
    def __init__(self, code: str, path: str, detail: str) -> None:
        self.code = code
        self.path = path
        self.detail = detail[:240]
        super().__init__(f"{path}: {self.detail}")


def _reject_float(_: str) -> None:
    raise RecipeContractError(
        "recipe.float_forbidden", "$", "floats are not permitted"
    )


def _reject_constant(_: str) -> None:
    raise RecipeContractError(
        "recipe.float_forbidden", "$", "floats are not permitted"
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RecipeContractError(
                "recipe.duplicate_key", "$", f"duplicate object key: {key}"
            )
        result[key] = value
    return result


def parse_recipe_json(payload: bytes | str) -> Mapping[str, object]:
    try:
        document = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except RecipeContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RecipeContractError(
            "recipe.invalid_json", "$", "recipe is not valid UTF-8 JSON"
        ) from error
    if not isinstance(document, dict):
        raise RecipeContractError(
            "recipe.object_required", "$", "recipe must be a JSON object"
        )
    return document


def _assert_canonical_value(value: object, path: str = "$") -> None:
    if isinstance(value, float):
        raise RecipeContractError(
            "recipe.float_forbidden", path, "floats are not permitted"
        )
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise RecipeContractError(
                    "recipe.key_type", path, "object keys must be strings"
                )
            _assert_canonical_value(child, f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _assert_canonical_value(child, f"{path}[{index}]")
        return
    raise RecipeContractError(
        "recipe.value_type", path, "recipe contains an unsupported value type"
    )


def canonical_recipe(document: Mapping[str, object]) -> bytes:
    _assert_canonical_value(document)
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def recipe_content_sha256(document: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_recipe(document)).hexdigest()


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    root = Path(__file__).resolve().parents[3]
    schema = json.loads(
        (root / "schemas/global/recipe-v1.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_recipe(document: Mapping[str, object]) -> None:
    errors = sorted(
        _validator().iter_errors(document),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if not errors:
        return
    error = _most_specific(errors[0])
    path = ".".join(str(part) for part in error.absolute_path) or "$"
    raise RecipeContractError(
        f"recipe.schema.{error.validator}", path, _safe_detail(error)
    )


def _most_specific(error: ValidationError) -> ValidationError:
    candidates = [error]
    pending = list(error.context)
    while pending:
        candidate = pending.pop()
        candidates.append(candidate)
        pending.extend(candidate.context)
    return max(
        candidates,
        key=lambda candidate: (
            candidate.validator == "required",
            len(candidate.absolute_path),
            -len(candidate.context),
        ),
    )


def _safe_detail(error: ValidationError) -> str:
    field = str(error.absolute_path[-1]) if error.absolute_path else "recipe"
    if error.validator == "required":
        missing = sorted(set(error.validator_value) - set(error.instance))
        return f"required field missing: {missing[0]}"
    if error.validator == "additionalProperties":
        allowed = set(error.schema.get("properties", {}))
        extra = sorted(set(error.instance) - allowed)
        return f"unexpected field: {extra[0] if extra else field}"
    if error.validator == "const":
        return f"{field} must equal {error.validator_value!r}"
    if error.validator == "pattern":
        return f"{field} does not match the required format"
    return f"{field} violates the {error.validator} constraint"
