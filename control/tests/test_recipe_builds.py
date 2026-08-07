from __future__ import annotations

import io
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from vonk_control.catalog_service import CatalogService, RecipeDraftInput
from vonk_control.inventory_repository import InventoryRepository, InventorySnapshotInput
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    Base,
    ClusterMapping,
    ClusterMappingNode,
    NodeArtifact,
    RecipeBuild,
    RecipeSourceBundle,
    ResourceReservation,
)
from vonk_control.recipe_builds import RecipeBuildService
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.source_bundles import SourceBundleStore, generate_source_bundle
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


class RecordingQueue:
    def enqueue_in_session(
        self,
        session,
        parent_job_id,
        node_id,
        operation,
        base_commit,
        payload,
        *,
        operation_id,
    ):
        session.add(
            AgentOperation(
                id=operation_id,
                parent_job_id=parent_job_id,
                node_id=node_id,
                kind=operation,
                payload_digest="f" * 64,
                payload=dict(payload),
                base_commit=base_commit,
                state="queued",
                current_attempt=0,
                created_at=datetime(2026, 8, 7, 12, tzinfo=UTC),
                updated_at=datetime(2026, 8, 7, 12, tzinfo=UTC),
            )
        )

    def notify_available(self) -> None:
        pass


def setup(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'build.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 8, 7, 12, tzinfo=UTC)
    node_id = "spk_" + "1" * 32
    bundle = generate_source_bundle(
        {
            "Dockerfile": (
                "FROM ghcr.io/example/vllm@sha256:" + "a" * 64 + "\nUSER 10001:10001\n"
            ).encode()
        }
    )
    bundles = SourceBundleStore(tmp_path / "bundles")
    stored = bundles.put(bundle.sha256, io.BytesIO(bundle.archive))
    document = json.loads(
        (Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json").read_text()
    )
    document["build"]["context"]["sha256"] = bundle.sha256
    document["build"]["context"]["expected_bytes"] = len(bundle.archive)
    document["build"]["resources"] = {
        "download_bytes": 100,
        "temporary_bytes": 200,
        "memory_bytes": 300,
        "timeout_seconds": 600,
    }
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=node_id,
                state="active",
                architecture="linux-arm64",
                agent_implementation="rust",
                migration_state="complete",
                capabilities=["recipe.build.v1"],
                last_seen_at=now,
            )
        )
        session.add(
            RecipeSourceBundle(
                sha256=bundle.sha256,
                media_type="application/vnd.vonk.source-bundle.v1+tar",
                archive_bytes=stored.archive_bytes,
                total_bytes=bundle.manifest.total_bytes,
                file_count=len(bundle.manifest.files),
                storage_key=f"{bundle.sha256[:2]}/{bundle.sha256}.tar",
                manifest={"files": [asdict(item) for item in bundle.manifest.files]},
                verified_at=now,
            )
        )
    InventoryRepository(sessions, clock=lambda: now).record(
        InventorySnapshotInput(
            node_id,
            now,
            100_000,
            90_000,
            100_000,
            80_000,
            100_000,
            80_000,
            1,
            False,
            ("recipe.build.v1",),
        )
    )
    catalog = CatalogService(sessions, clock=lambda: now)
    draft = catalog.create_recipe(
        "admin", RecipeDraftInput(slug="qwen3-vllm", document=document)
    )
    revision = catalog.resolve(draft.recipe_id, 1, "admin")
    return sessions, bundles, now, node_id, revision


def test_build_plan_is_typed_sandboxed_and_durable(tmp_path: Path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)

    plan = RecipeBuildService(sessions, bundles=bundles).plan(
        revision.id, node_id, now=now
    )

    assert plan.agent_payload["kind"] == "recipe.build.v1"
    assert "command" not in plan.agent_payload
    assert plan.agent_payload["limits"]["gpu"] == 0
    assert (
        plan.agent_payload["source_bundle_sha256"]
        == revision.document["build"]["context"]["sha256"]
    )
    with sessions() as session:
        stored = session.get(RecipeBuild, plan.build_id)
        assert stored is not None and stored.state == "planned"


def test_starting_build_atomically_reserves_temporary_disk_and_memory(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    builds = RecipeBuildService(sessions, bundles=bundles)
    plan = builds.plan(revision.id, node_id, now=now)
    operations = RecipeOperationService(
        sessions,
        install_admission=object(),
        run_admission=object(),
        agent_jobs=RecordingQueue(),
        clock=lambda: now,
        builds=builds,
    )

    operation = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="build-reservation-test",
    )

    with sessions() as session:
        reservations = tuple(
            session.scalars(
                select(ResourceReservation)
                .where(
                    ResourceReservation.owner_kind == "recipe-build",
                    ResourceReservation.owner_id == plan.build_id,
                    ResourceReservation.state == "active",
                )
                .order_by(ResourceReservation.kind)
            )
        )
    assert [(item.kind, item.amount_bytes) for item in reservations] == [
        (
            "disk",
            plan.agent_payload["limits"]["temporary_bytes"]
            + plan.agent_payload["source_bundle_bytes"],
        ),
        ("host-memory", plan.agent_payload["limits"]["memory_bytes"]),
    ]

    operations.record_node_result(
        operation.id,
        node_id,
        succeeded=False,
        evidence={"reason": "expected test failure"},
    )
    with sessions() as session:
        assert (
            session.scalar(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "recipe-build",
                    ResourceReservation.owner_id == plan.build_id,
                    ResourceReservation.state == "active",
                )
            )
            is None
        )


def test_source_check_returns_the_structured_pre_dispatch_policy_report(
    tmp_path: Path,
) -> None:
    sessions, bundles, _now, _node_id, revision = setup(tmp_path)
    # The check is independent of builder capacity and exposes every finding to the UI.
    report = RecipeBuildService(sessions, bundles=bundles).check_source(revision.id)

    assert report.passed is True
    assert report.findings == ()
    assert report.source_bundle_sha256


def test_success_records_one_exact_image_on_builder(tmp_path: Path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    plan = service.plan(revision.id, node_id, now=now)

    completed = service.record_success(
        plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        image_digest="sha256:" + "b" * 64,
        oci_layout_sha256="c" * 64,
        image_bytes=500,
        now=now,
    )

    assert completed.image_digest == "sha256:" + "b" * 64
    with sessions() as session:
        artifact = session.scalar(
            select(NodeArtifact).where(NodeArtifact.node_id == node_id)
        )
        assert artifact is not None and artifact.digest == "b" * 64


def test_distribution_uses_one_build_digest_for_every_missing_node(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, builder, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    plan = service.plan(revision.id, builder, now=now)
    service.record_success(
        plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        image_digest="sha256:" + "b" * 64,
        oci_layout_sha256="c" * 64,
        image_bytes=500,
        now=now,
    )
    target = "spk_" + "2" * 32
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=target,
                state="active",
                architecture="linux-arm64",
                capabilities=["recipe.image.import.v1"],
            )
        )
        mapping = ClusterMapping(
            recipe_revision_id=revision.id,
            profile_name="synthetic-test",
            generation=1,
            node_count=2,
            state="ready",
            parameters={},
            placement_digest="d" * 64,
            endpoint_owner_node_id=builder,
            created_by="admin",
            created_at=now,
            updated_at=now,
        )
        session.add(mapping)
        session.flush()
        session.add_all(
            (
                ClusterMappingNode(
                    mapping_id=mapping.id,
                    node_id=builder,
                    rank=0,
                    role="entrypoint",
                    endpoint_owner=True,
                    created_at=now,
                ),
                ClusterMappingNode(
                    mapping_id=mapping.id,
                    node_id=target,
                    rank=1,
                    role="worker",
                    endpoint_owner=False,
                    created_at=now,
                ),
            )
        )
        mapping_id = mapping.id

    distribution = service.plan_distribution(plan.build_id, mapping_id, generation=1)

    assert [item[0] for item in distribution.targets] == [target]
    assert {item[1]["image_digest"] for item in distribution.targets} == {
        "sha256:" + "b" * 64
    }
    assert distribution.targets[0][1]["kind"] == "recipe.image.import.v1"
