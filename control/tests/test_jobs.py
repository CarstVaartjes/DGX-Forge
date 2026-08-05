from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from dgx_agent_protocol import canonical_message
from dgx_control.jobs import JobService, StaleAttempt
from dgx_control.models import Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 3, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def service(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'jobs.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    clock = Clock()
    return JobService(sessionmaker(engine, expire_on_commit=False), clock=clock), clock


def test_workers_cannot_claim_same_job(service) -> None:
    jobs, _ = service
    job = jobs.enqueue("probe", "admin", "abc123", ["spk_1"], {"safe": True})
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda index: jobs.claim(f"worker-{index}", 30), range(4)))
    claimed = [claim for claim in claims if claim is not None]
    assert len(claimed) == 1
    assert claimed[0].job_id == job.id


def test_job_list_keyset_pages_reach_every_job_in_stable_order(service) -> None:
    jobs, clock = service
    expected: set[str] = set()
    for index in range(23):
        expected.add(
            jobs.enqueue(
                "probe", "admin", "a" * 40, [f"spk_{index:032x}"], {}
            ).id
        )
        if index % 3 == 0:
            clock.now += timedelta(seconds=1)

    found: list[str] = []
    cursor = None
    while True:
        page, cursor, total = jobs.list_page(limit=7, cursor=cursor)
        found.extend(job.id for job in page)
        assert total == 23
        if cursor is None:
            break

    assert len(found) == len(set(found)) == 23
    assert set(found) == expected
    with pytest.raises(ValueError, match="cursor"):
        jobs.list_page(limit=7, cursor="not-a-cursor")


def test_claim_carries_commit_and_targets_to_the_worker(service) -> None:
    jobs, _ = service
    jobs.enqueue("probe", "admin", "a" * 40, ["spk_a", "spk_b"], {})

    attempt = jobs.claim("worker", 30)

    assert attempt is not None
    assert attempt.base_commit == "a" * 40
    assert attempt.targets == ("spk_a", "spk_b")


def test_stale_attempt_cannot_publish_success_after_lease_reclaim(service) -> None:
    jobs, clock = service
    jobs.enqueue("probe", "admin", "abc123", ["spk_1"], {})
    first = jobs.claim("worker-1", 10)
    assert first is not None
    clock.now += timedelta(seconds=11)
    second = jobs.claim("worker-2", 30)
    assert second is not None and second.fence != first.fence
    with pytest.raises(StaleAttempt):
        jobs.succeed(first, {"wrong": True})
    jobs.succeed(second, {"ok": True})
    assert jobs.get(second.job_id).state == "succeeded"


def test_payload_is_bounded_and_rejects_credential_fields(service) -> None:
    jobs, _ = service
    with pytest.raises(ValueError, match="sensitive"):
        jobs.enqueue("probe", "admin", "abc", [], {"password": "no"})
    with pytest.raises(ValueError, match="large"):
        jobs.enqueue("probe", "admin", "abc", [], {"value": "x" * 70_000})
    with pytest.raises(TypeError, match="keys"):
        jobs.enqueue("probe", "admin", "abc", [], {1: "not-a-string-key"})



@pytest.mark.parametrize(
    "payload",
    [
        {"tokens_per_minute": "nested-secret"},
        {"safe": {"tokens_per_minute": 10_000}},
        {
            "routes": {
                "chat": {
                    "quota": {
                        "requests_per_minute": 30,
                        "tokens_per_minute": "nested-secret",
                    }
                }
            }
        },
    ],
)
def test_token_named_fields_outside_validated_route_quota_are_sensitive(
    service, payload: dict[str, object]
) -> None:
    jobs, _ = service

    with pytest.raises(ValueError, match="sensitive"):
        jobs.enqueue("reconcile", "admin", "abc", ["spk_1"], payload)


def test_exact_bounded_reconciliation_route_quota_is_accepted(service) -> None:
    jobs, _ = service
    quota = {"requests_per_minute": 30, "tokens_per_minute": 10_000}
    node_id = "spk_00000000000000000000000000000001"
    payload = {
        "routes": {
            "chat": {
                "workload_id": "model",
                "nodes": [node_id],
                "entrypoint_node_id": node_id,
                "scheme": "http",
                "port": 8000,
                "path": "/v1",
                "quota": quota,
                "quota_digest": hashlib.sha256(canonical_message(quota)).hexdigest(),
            }
        }
    }

    accepted = jobs.enqueue(
        "reconcile",
        "admin",
        "abc",
        [node_id],
        payload,
    )
    assert accepted.payload == payload


def test_matching_fence_can_heartbeat_wait_and_fail(service) -> None:
    jobs, _ = service
    jobs.enqueue("install", "operator", "abc", ["spk_1"], {})
    attempt = jobs.claim("worker", 10)
    assert attempt is not None
    renewed = jobs.heartbeat(attempt, 20)
    assert renewed.lease_deadline > attempt.lease_deadline
    jobs.wait_for_operator(renewed, "confirm console fingerprint")
    assert jobs.get(renewed.job_id).state == "waiting-for-operator"

    jobs.resume(renewed.job_id)
    retry = jobs.claim("worker", 10)
    assert retry is not None
    jobs.fail(retry, "bounded failure")
    assert jobs.get(retry.job_id).state == "failed"


def test_concurrent_operator_resume_has_one_winner(service) -> None:
    jobs, _ = service
    jobs.enqueue("install", "operator", "abc", ["spk_1"], {})
    attempt = jobs.claim("worker", 10)
    assert attempt is not None
    jobs.wait_for_operator(attempt, "confirm console fingerprint")

    def resume() -> str:
        try:
            jobs.resume(attempt.job_id)
            return "won"
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _index: resume(), range(8)))

    assert outcomes.count("won") == 1
    assert outcomes.count("conflict") == 7
