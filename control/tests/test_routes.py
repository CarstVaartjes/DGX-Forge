from datetime import UTC, datetime
from pathlib import Path

import pytest
from dgx_control.routes import RouteCandidate, RoutePublisher, RouteValidationError


def _candidate(upstream="http://node-a.internal:8000", aliases=("deepseek", "reasoning")):
    return RouteCandidate(
        commit="a" * 40, profile="agent", workload="deepseek", node_ids=("spk_00000000000000000000000000000001",),
        aliases={alias: upstream for alias in aliases},
        health_timestamp=datetime(2026, 8, 3, tzinfo=UTC),
    )


def test_invalid_candidate_keeps_maintenance_routes(tmp_path: Path) -> None:
    applied = []
    publisher = RoutePublisher(tmp_path, allowed_upstreams={"http://node-a.internal:8000"}, validate=lambda content: True, apply=lambda content: applied.append(content))
    publisher.maintenance(("spk_00000000000000000000000000000001",), "switch")
    with pytest.raises(RouteValidationError, match="unconfigured"):
        publisher.publish(_candidate(upstream="http://shadow:8888"))
    assert publisher.snapshot().state == "maintenance"
    assert publisher.visible_aliases() == set()
    assert len(applied) == 1


def test_publish_is_atomic_for_all_profile_aliases(tmp_path: Path) -> None:
    applied = []
    publisher = RoutePublisher(tmp_path, allowed_upstreams={"http://node-a.internal:8000"}, validate=lambda content: b"reasoning" in content, apply=lambda content: applied.append(content))
    state = publisher.publish(_candidate())
    assert state.generation == 1
    assert publisher.visible_aliases() == {"deepseek", "reasoning"}
    assert len(applied) == 1 and b"deepseek" in applied[0]


def test_validator_or_apply_failure_keeps_previous_generation(tmp_path: Path) -> None:
    fail = False
    def apply(_content):
        if fail: raise RuntimeError("Caddy rejected generation")
    publisher = RoutePublisher(tmp_path, allowed_upstreams={"http://node-a.internal:8000"}, validate=lambda content: True, apply=apply)
    first = publisher.publish(_candidate(aliases=("deepseek",)))
    fail = True
    with pytest.raises(RouteValidationError, match="apply"):
        publisher.publish(_candidate(aliases=("reasoning",)))
    assert publisher.snapshot() == first
    assert publisher.visible_aliases() == {"deepseek"}


def test_route_state_rejects_symlink_root(tmp_path: Path) -> None:
    actual = tmp_path / "actual"; actual.mkdir()
    link = tmp_path / "routes"; link.symlink_to(actual)
    with pytest.raises(RouteValidationError, match="symlink"):
        RoutePublisher(link, allowed_upstreams=set(), validate=lambda _: True, apply=lambda _: None)
