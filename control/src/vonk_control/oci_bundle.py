"""Exact, bounded acquisition of a TUF-authorized OCI deployment bundle."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cluster_profiles.platform_release import OciDeploymentBundle

from .host_commands import (
    ArtifactPolicy,
    ArtifactReceipt,
    BoundedCommandRunner,
    CommandPolicy,
    HostCommandError,
)

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REFERENCE = re.compile(
    r"[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9][a-z0-9._/-]*"
    r"@sha256:[0-9a-f]{64}\Z"
)
_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
_LAYER_MEDIA_TYPE = "application/vnd.dgx-forge.control-deployment.v1.tar"
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_LAYER_BYTES = 64 * 1024 * 1024
_DEFAULT_COMMAND = CommandPolicy(15 * 60, 1024 * 1024, 1024 * 1024)


class OciBundleError(RuntimeError):
    """The exact authorized OCI deployment bundle could not be acquired."""


class OciBundleSource:
    """Fetch raw manifest and layer bytes by their exact OCI digests.

    ``fetch_to`` is the primary large-artifact boundary. It sends ORAS output
    directly to a caller-owned regular file descriptor and returns only a
    count/digest receipt. ``fetch`` is the compatibility convenience required
    by the control-upgrade interface and materializes bytes only after the
    streamed artifact has passed every descriptor check.
    """

    def __init__(
        self,
        *,
        oras_path: Path,
        work_directory: Path,
        runner: BoundedCommandRunner | None = None,
        environment: Mapping[str, str] | None = None,
        command_policy: CommandPolicy = _DEFAULT_COMMAND,
        required_free_bytes: int = 0,
    ) -> None:
        executable = Path(oras_path)
        work = Path(work_directory)
        try:
            executable_info = executable.lstat()
        except OSError as error:
            raise OciBundleError("ORAS executable is unavailable") from error
        if (
            not executable.is_absolute()
            or not stat.S_ISREG(executable_info.st_mode)
            or stat.S_ISLNK(executable_info.st_mode)
            or not executable_info.st_mode & 0o111
        ):
            raise OciBundleError("ORAS executable is invalid")
        try:
            resolved_work = work.resolve(strict=True)
        except OSError as error:
            raise OciBundleError("OCI work directory is unavailable") from error
        if (
            not work.is_absolute()
            or work.is_symlink()
            or not work.is_dir()
            or resolved_work != work
        ):
            raise OciBundleError("OCI work directory is invalid")
        if (
            isinstance(required_free_bytes, bool)
            or not isinstance(required_free_bytes, int)
            or not 0 <= required_free_bytes <= 16 * 1024**4
        ):
            raise ValueError("OCI required free bytes is invalid")
        self._oras = executable
        self._work = work
        self._runner = runner if runner is not None else BoundedCommandRunner()
        self._environment = dict(environment or {})
        self._command = command_policy
        self._required_free_bytes = required_free_bytes

    def fetch(self, descriptor: OciDeploymentBundle) -> bytes:
        """Fetch and return a verified bundle for the byte-oriented upgrader API."""

        with self._scratch_file("bundle") as sink_fd:
            receipt = self.fetch_to(descriptor, sink_fd)
            return _read_exact(sink_fd, receipt.byte_count)

    def fetch_to(
        self, descriptor: OciDeploymentBundle, sink_fd: int
    ) -> ArtifactReceipt:
        """Stream a verified bundle layer into an empty preopened file."""

        repository = _validate_descriptor(descriptor)
        _validate_sink(sink_fd)
        manifest = self._fetch_manifest(descriptor)
        _validate_manifest(manifest, descriptor)

        layer_reference = f"{repository}@{descriptor.layer_digest}"
        try:
            receipt = self._runner.stream(
                (
                    str(self._oras),
                    "blob",
                    "fetch",
                    "--output",
                    "-",
                    layer_reference,
                ),
                cwd=self._work,
                env=self._environment,
                source_fd=None,
                sink_fd=sink_fd,
                command=self._command,
                artifact=ArtifactPolicy(
                    descriptor.layer_size, self._required_free_bytes
                ),
            )
        except HostCommandError as error:
            raise _command_failure("layer", error) from error
        if receipt.byte_count != descriptor.layer_size:
            _clear_sink(sink_fd)
            raise OciBundleError("OCI layer size mismatch")
        if "sha256:" + receipt.sha256 != descriptor.layer_digest:
            _clear_sink(sink_fd)
            raise OciBundleError("OCI layer digest mismatch")
        return receipt

    def _fetch_manifest(self, descriptor: OciDeploymentBundle) -> bytes:
        with self._scratch_file("manifest") as sink_fd:
            try:
                receipt = self._runner.stream(
                    (
                        str(self._oras),
                        "manifest",
                        "fetch",
                        "--output",
                        "-",
                        "--media-type",
                        descriptor.manifest_media_type,
                        descriptor.reference,
                    ),
                    cwd=self._work,
                    env=self._environment,
                    source_fd=None,
                    sink_fd=sink_fd,
                    command=self._command,
                    artifact=ArtifactPolicy(
                        descriptor.manifest_size, self._required_free_bytes
                    ),
                )
            except HostCommandError as error:
                raise _command_failure("manifest", error) from error
            if receipt.byte_count != descriptor.manifest_size:
                raise OciBundleError("OCI manifest size mismatch")
            if "sha256:" + receipt.sha256 != descriptor.manifest_digest:
                raise OciBundleError("OCI manifest digest mismatch")
            return _read_exact(sink_fd, receipt.byte_count)

    @contextmanager
    def _scratch_file(self, purpose: str) -> Iterator[int]:
        directory_fd = -1
        descriptor = -1
        name = f".dgx-oci-{purpose}-{uuid.uuid4().hex}.part"
        try:
            directory_fd = os.open(
                self._work,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            descriptor = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            yield descriptor
        except OciBundleError:
            raise
        except OSError as error:
            raise OciBundleError("OCI scratch file failure") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if directory_fd >= 0:
                try:
                    os.unlink(name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                finally:
                    os.close(directory_fd)


def _validate_descriptor(descriptor: OciDeploymentBundle) -> str:
    if not isinstance(descriptor, OciDeploymentBundle):
        raise OciBundleError("OCI deployment bundle descriptor is invalid")
    if (
        _REFERENCE.fullmatch(descriptor.reference) is None
        or _DIGEST.fullmatch(descriptor.manifest_digest) is None
        or _DIGEST.fullmatch(descriptor.layer_digest) is None
    ):
        raise OciBundleError("OCI deployment bundle descriptor is invalid")
    repository, reference_digest = descriptor.reference.rsplit("@", 1)
    if reference_digest != descriptor.manifest_digest:
        raise OciBundleError("OCI deployment bundle descriptor is unbound")
    if (
        isinstance(descriptor.manifest_size, bool)
        or not isinstance(descriptor.manifest_size, int)
        or not 1 <= descriptor.manifest_size <= _MAX_MANIFEST_BYTES
    ):
        raise OciBundleError("OCI manifest size is invalid")
    if descriptor.manifest_media_type != _MANIFEST_MEDIA_TYPE:
        raise OciBundleError("OCI manifest media type is invalid")
    if (
        isinstance(descriptor.layer_size, bool)
        or not isinstance(descriptor.layer_size, int)
        or not 1 <= descriptor.layer_size <= _MAX_LAYER_BYTES
    ):
        raise OciBundleError("OCI layer size is invalid")
    if descriptor.layer_media_type != _LAYER_MEDIA_TYPE:
        raise OciBundleError("OCI layer media type is invalid")
    return repository


def _validate_sink(descriptor: int) -> None:
    if (
        isinstance(descriptor, bool)
        or not isinstance(descriptor, int)
        or descriptor < 0
    ):
        raise OciBundleError("OCI bundle sink is invalid")
    try:
        info = os.fstat(descriptor)
        offset = os.lseek(descriptor, 0, os.SEEK_CUR)
    except OSError as error:
        raise OciBundleError("OCI bundle sink is unavailable") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size != 0
        or offset != 0
    ):
        raise OciBundleError("OCI bundle sink is unsafe")


def _validate_manifest(raw: bytes, descriptor: OciDeploymentBundle) -> None:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_nonfinite(_value: str) -> None:
        raise ValueError("non-finite number")

    try:
        document = json.loads(
            raw,
            object_pairs_hook=unique_object,
            parse_constant=reject_nonfinite,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as error:
        raise OciBundleError("OCI manifest JSON is invalid") from error
    if not isinstance(document, dict):
        raise OciBundleError("OCI manifest JSON is invalid")
    if document.get("schemaVersion") != 2:
        raise OciBundleError("OCI manifest schema version mismatch")
    if document.get("mediaType") != descriptor.manifest_media_type:
        raise OciBundleError("OCI manifest media type mismatch")
    layers = document.get("layers")
    if not isinstance(layers, list) or len(layers) != 1:
        raise OciBundleError("OCI layer descriptor mismatch")
    layer = layers[0]
    if not isinstance(layer, dict) or (
        layer.get("digest") != descriptor.layer_digest
        or layer.get("size") != descriptor.layer_size
        or layer.get("mediaType") != descriptor.layer_media_type
    ):
        raise OciBundleError("OCI layer descriptor mismatch")


def _read_exact(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    offset = 0
    while offset < size:
        try:
            chunk = os.pread(descriptor, min(1024 * 1024, size - offset), offset)
        except OSError as error:
            raise OciBundleError("OCI artifact read failure") from error
        if not chunk:
            raise OciBundleError("OCI artifact changed after verification")
        chunks.append(chunk)
        offset += len(chunk)
    try:
        if os.pread(descriptor, 1, offset):
            raise OciBundleError("OCI artifact changed after verification")
    except OSError as error:
        raise OciBundleError("OCI artifact read failure") from error
    return b"".join(chunks)


def _clear_sink(descriptor: int) -> None:
    try:
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError as error:
        raise OciBundleError("OCI incomplete sink cleanup failed") from error


def _command_failure(kind: str, error: HostCommandError) -> OciBundleError:
    if error.reason == "timeout":
        return OciBundleError(f"OCI {kind} command timeout")
    if "artifact" in error.reason or "output limit" in error.reason:
        return OciBundleError(f"OCI {kind} size or command output limit exceeded")
    return OciBundleError(f"OCI {kind} command failed")
