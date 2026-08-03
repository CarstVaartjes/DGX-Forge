"""Policy-limited LiteLLM configuration derived only from published routes."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .routes import RouteState


class LiteLlmPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class LiteLlmPolicy:
    models: Mapping[str, Mapping[str, int]]


@dataclass(frozen=True)
class LiteLlmGeneration:
    generation: int
    route_digest: str
    config_sha256: str
    path: str


class LiteLlmPublisher:
    def __init__(self, root: Path, *, validate: Callable[[bytes], bool], apply: Callable[[bytes], None]) -> None:
        if root.is_symlink():
            raise LiteLlmPolicyError("LiteLLM state root must not be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._root = root
        self._validate = validate
        self._apply = apply

    def render(self, routes: RouteState, policy: LiteLlmPolicy) -> bytes:
        if routes.state != "published" or not routes.aliases:
            raise LiteLlmPolicyError("LiteLLM models require a published route snapshot")
        models = dict(policy.models)
        unknown = set(models) - set(routes.aliases)
        if unknown:
            raise LiteLlmPolicyError("LiteLLM policy contains models outside published aliases")
        if not models:
            raise LiteLlmPolicyError("LiteLLM policy must publish at least one model")
        model_list = []
        for alias in sorted(models):
            quota = dict(models[alias])
            if set(quota) != {"requests_per_minute", "tokens_per_minute"}:
                raise LiteLlmPolicyError("LiteLLM model quota fields are invalid")
            rpm, tpm = quota["requests_per_minute"], quota["tokens_per_minute"]
            if not isinstance(rpm, int) or not isinstance(tpm, int) or not 1 <= rpm <= 100_000 or not 1 <= tpm <= 100_000_000:
                raise LiteLlmPolicyError("LiteLLM model quotas are outside allowed bounds")
            model_list.append({
                "model_name": alias,
                "litellm_params": {
                    "model": f"openai/{alias}",
                    "api_base": routes.aliases[alias].rstrip("/"),
                    "api_key": "os.environ/LITELLM_UPSTREAM_KEY",
                    "rpm": rpm,
                    "tpm": tpm,
                },
            })
        document = {
            "general_settings": {
                "database_url": "os.environ/LITELLM_DATABASE_URL",
                "disable_admin_ui": True,
                "master_key": "os.environ/LITELLM_MASTER_KEY",
            },
            "litellm_settings": {
                "drop_params": True,
                "set_verbose": False,
                "success_callback": [],
                "failure_callback": [],
            },
            "model_list": model_list,
            "router_settings": {"enable_pre_call_checks": True, "routing_strategy": "simple-shuffle"},
        }
        return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()

    def publish(self, routes: RouteState, policy: LiteLlmPolicy) -> LiteLlmGeneration:
        content = self.render(routes, policy)
        if self._validate(content) is not True:
            raise LiteLlmPolicyError("LiteLLM candidate failed validation")
        current = self.active(optional=True)
        number = (current.generation if current else 0) + 1
        digest = hashlib.sha256(content).hexdigest()
        directory = self._root / f"{number:08d}-{digest}"
        try:
            directory.mkdir(mode=0o700)
            target = directory / "config.yaml"
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(content); output.flush(); os.fsync(output.fileno())
            self._apply(content)
        except LiteLlmPolicyError:
            raise
        except Exception as error:
            raise LiteLlmPolicyError("LiteLLM candidate apply failed; previous generation retained") from error
        generation = LiteLlmGeneration(number, routes.digest, digest, str(target))
        pointer = (json.dumps(generation.__dict__, sort_keys=True, separators=(",", ":")) + "\n").encode()
        descriptor, temporary_raw = tempfile.mkstemp(prefix=".active-", dir=self._root)
        temporary = Path(temporary_raw)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(pointer); output.flush(); os.fsync(output.fileno())
            os.replace(temporary, self._root / "active.json")
        finally:
            temporary.unlink(missing_ok=True)
        return generation

    def active(self, *, optional: bool = False) -> LiteLlmGeneration | None:
        pointer = self._root / "active.json"
        if not pointer.exists():
            if optional:
                return None
            raise LiteLlmPolicyError("no LiteLLM generation is active")
        if pointer.is_symlink() or not pointer.is_file():
            raise LiteLlmPolicyError("LiteLLM active pointer is unsafe")
        try:
            raw = json.loads(pointer.read_bytes())
            generation = LiteLlmGeneration(raw["generation"], raw["route_digest"], raw["config_sha256"], raw["path"])
            config = Path(generation.path)
            if config.is_symlink() or not config.is_file() or hashlib.sha256(config.read_bytes()).hexdigest() != generation.config_sha256:
                raise LiteLlmPolicyError("LiteLLM active generation checksum mismatch")
            return generation
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise LiteLlmPolicyError("LiteLLM active generation is unreadable") from error
