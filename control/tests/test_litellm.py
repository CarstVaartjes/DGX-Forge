import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dgx_control.litellm import LiteLlmPolicy, LiteLlmPolicyError, LiteLlmPublisher
from dgx_control.routes import RouteState


def _snapshot():
    return RouteState(1, "published", "a" * 40, "agent", "deepseek", ("spk_00000000000000000000000000000001",), {"deepseek": "http://node.internal:8000/v1"}, datetime(2026, 8, 3, tzinfo=UTC).isoformat(), None, "b" * 64)


def _policy(models=("deepseek",)):
    return LiteLlmPolicy(models={model: {"requests_per_minute": 30, "tokens_per_minute": 10000} for model in models})


def test_litellm_cannot_add_unknown_repository_model(tmp_path: Path) -> None:
    publisher = LiteLlmPublisher(tmp_path, validate=lambda _: True, apply=lambda _: None)
    with pytest.raises(LiteLlmPolicyError, match="published aliases"):
        publisher.render(_snapshot(), _policy(("deepseek", "shadow-model")))


def test_rendered_config_contains_secret_references_not_values(tmp_path: Path) -> None:
    publisher = LiteLlmPublisher(tmp_path, validate=lambda _: True, apply=lambda _: None)
    rendered = publisher.render(_snapshot(), _policy())
    decoded = json.loads(rendered)
    assert decoded["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY"
    assert decoded["model_list"][0]["litellm_params"]["api_key"] == "os.environ/LITELLM_UPSTREAM_KEY"
    assert b"sk-live" not in rendered
    assert decoded["model_list"][0]["model_name"] == "deepseek"


def test_apply_is_atomic_and_retains_previous_generation(tmp_path: Path) -> None:
    applied = []
    publisher = LiteLlmPublisher(tmp_path, validate=lambda content: b"deepseek" in content, apply=lambda content: applied.append(content))
    generation = publisher.publish(_snapshot(), _policy())
    assert publisher.active() == generation
    rejecting = LiteLlmPublisher(tmp_path, validate=lambda _: False, apply=lambda _: None)
    with pytest.raises(LiteLlmPolicyError, match="validation"):
        rejecting.publish(_snapshot(), _policy())
    assert rejecting.active() == generation


def test_maintenance_snapshot_cannot_render_models(tmp_path: Path) -> None:
    snapshot = _snapshot()
    maintenance = RouteState(snapshot.generation, "maintenance", None, None, None, snapshot.node_ids, {}, None, "switch", snapshot.digest)
    with pytest.raises(LiteLlmPolicyError, match="published"):
        LiteLlmPublisher(tmp_path, validate=lambda _: True, apply=lambda _: None).render(maintenance, _policy())
