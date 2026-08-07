"""Typed per-selection environment for verified control generations."""

from __future__ import annotations

import re
from dataclasses import dataclass

_IDENTIFIER = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IMAGE = re.compile(r"[^\s]{1,1900}@sha256:[0-9a-f]{64}\Z")
_NONCE = re.compile(r"[0-9a-f]{64}\Z")


class GenerationLaunchError(ValueError):
    """A generation cannot be launched with the supplied exact identity."""


@dataclass(frozen=True)
class GenerationReleaseIdentity:
    generation_id: str
    database_revision: str
    platform_version: str
    release_digest: str
    build_digest: str
    api_image: str
    worker_image: str

    def __post_init__(self) -> None:
        if (
            _IDENTIFIER.fullmatch(self.generation_id) is None
            or _IDENTIFIER.fullmatch(self.database_revision) is None
            or _SEMVER.fullmatch(self.platform_version) is None
            or _DIGEST.fullmatch(self.release_digest) is None
            or _DIGEST.fullmatch(self.build_digest) is None
            or _IMAGE.fullmatch(self.api_image) is None
            or _IMAGE.fullmatch(self.worker_image) is None
        ):
            raise GenerationLaunchError("generation release identity is invalid")


@dataclass(frozen=True)
class SelectionRuntime:
    mode: str
    start_nonce: str
    operation_id: str | None

    @classmethod
    def selected(cls, start_nonce: str) -> SelectionRuntime:
        runtime = cls("selected", start_nonce, None)
        _validate_selected_runtime(runtime)
        return runtime


def _validate_selected_runtime(runtime: SelectionRuntime) -> None:
    if (
        type(runtime) is not SelectionRuntime
        or runtime.mode != "selected"
        or _NONCE.fullmatch(runtime.start_nonce) is None
        or runtime.operation_id is not None
    ):
        raise GenerationLaunchError("selected generation runtime is invalid")


def selected_compose_environment(
    identity: GenerationReleaseIdentity,
    runtime: SelectionRuntime,
) -> dict[str, str]:
    """Build exact Compose input; the caller must journal the fresh nonce."""
    if type(identity) is not GenerationReleaseIdentity:
        raise GenerationLaunchError("generation release identity is invalid")
    _validate_selected_runtime(runtime)
    return {
        "CONTROL_API_IMAGE": identity.api_image,
        "CONTROL_WORKER_IMAGE": identity.worker_image,
        "DGX_CONTROL_GENERATION_ID": identity.generation_id,
        "DGX_CONTROL_START_NONCE": runtime.start_nonce,
        "DGX_CONTROL_STARTUP_MODE": "selected",
        "DGX_DATABASE_REVISION": identity.database_revision,
        "DGX_PLATFORM_BUILD_DIGEST": identity.build_digest,
        "DGX_PLATFORM_RELEASE_DIGEST": identity.release_digest,
        "DGX_PLATFORM_VERSION": identity.platform_version,
    }
