from __future__ import annotations

from dataclasses import replace

import pytest
from dgx_control.generation_launch import (
    GenerationLaunchError,
    GenerationReleaseIdentity,
    SelectionRuntime,
    selected_compose_environment,
)


def _identity() -> GenerationReleaseIdentity:
    return GenerationReleaseIdentity(
        generation_id="gen-" + "a" * 24,
        database_revision="0012_control_process_heartbeats",
        platform_version="1.2.3",
        release_digest="sha256:" + "b" * 64,
        build_digest="sha256:" + "c" * 64,
        api_image="registry.example/api@sha256:" + "d" * 64,
        worker_image="registry.example/worker@sha256:" + "e" * 64,
    )


def test_selected_launch_requires_an_explicit_fresh_shared_nonce() -> None:
    identity = _identity()
    first = SelectionRuntime.selected("1" * 64)
    second = SelectionRuntime.selected("2" * 64)

    first_environment = selected_compose_environment(identity, first)
    second_environment = selected_compose_environment(identity, second)

    assert first_environment == {
        "CONTROL_API_IMAGE": identity.api_image,
        "CONTROL_WORKER_IMAGE": identity.worker_image,
        "DGX_CONTROL_GENERATION_ID": identity.generation_id,
        "DGX_CONTROL_START_NONCE": "1" * 64,
        "DGX_CONTROL_STARTUP_MODE": "selected",
        "DGX_DATABASE_REVISION": identity.database_revision,
        "DGX_PLATFORM_BUILD_DIGEST": identity.build_digest,
        "DGX_PLATFORM_RELEASE_DIGEST": identity.release_digest,
        "DGX_PLATFORM_VERSION": identity.platform_version,
    }
    assert second_environment["DGX_CONTROL_START_NONCE"] == "2" * 64


@pytest.mark.parametrize(
    "runtime",
    (
        SelectionRuntime("selected", "", None),
        SelectionRuntime("selected", "1" * 63, None),
        SelectionRuntime("selected", "1" * 64, "operation-1"),
        SelectionRuntime("preselection", "1" * 64, None),
    ),
)
def test_selection_runtime_rejects_missing_stale_or_cross_mode_bindings(
    runtime: SelectionRuntime,
) -> None:
    with pytest.raises(GenerationLaunchError):
        selected_compose_environment(_identity(), runtime)


def test_generation_release_identity_rejects_a_mutable_image_reference() -> None:
    with pytest.raises(GenerationLaunchError):
        replace(_identity(), worker_image="registry.example/worker:latest")
