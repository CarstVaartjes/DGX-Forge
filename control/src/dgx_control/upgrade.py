"""Recoverable host-local control-plane generation upgrades."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from spark_profiles.platform_release import PlatformRelease

_GENERATION = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_MAX_RENDERED_COMPOSE = 1024 * 1024


class UpgradeError(RuntimeError):
    """A control generation upgrade failed safely."""


class UpgradeConflict(UpgradeError):
    """The upgrade cannot start because its plan or host state is stale."""


class UpgradeReadinessError(UpgradeError):
    """The candidate control API did not become ready."""


class AmbiguousMigrationError(UpgradeError):
    """The database migration outcome cannot be determined automatically."""


class UpgradeRecoveryRequired(UpgradeError):
    """Automatic rollback is unsafe and an administrator must recover."""


@dataclass(frozen=True)
class ControlGenerationPlan:
    schema_version: int
    generation_id: str
    release_digest: str
    build_digest: str
    platform_version: str
    api_image: str
    worker_image: str
    migration_revision: str
    previous_generation: str | None
    required_bytes: int
    plan_digest: str


@dataclass(frozen=True)
class ControlGenerationResult:
    generation_id: str
    release_digest: str
    build_digest: str
    previous_generation: str | None
    status: str


class UpgradeBoundary(Protocol):
    def control_is_running(self) -> bool: ...

    def available_bytes(self) -> int: ...

    def pull(self, references: tuple[str, ...]) -> None: ...

    def render_compose(self, environment: dict[str, str]) -> bytes: ...

    def backup(self, generation_id: str) -> dict[str, object]: ...

    def stop_worker(self) -> None: ...

    def migrate(self, revision: str) -> None: ...

    def start_api(self, generation_path: Path) -> None: ...

    def readiness(self) -> dict[str, object]: ...

    def start_worker(self) -> None: ...

    def stop_api(self) -> None: ...

    def restore_generation(self, generation_path: Path) -> None: ...


class ControlUpgrade:
    """Plan and apply one immutable control-host generation."""

    def __init__(self, state_root: Path, boundary: UpgradeBoundary) -> None:
        self._state_root = Path(state_root)
        if not self._state_root.is_absolute():
            raise UpgradeConflict("upgrade state root must be absolute")
        self._boundary = boundary

    def plan(self, release: PlatformRelease) -> ControlGenerationPlan:
        previous = _active_generation(self._state_root)
        content = {
            "schema_version": 1,
            "generation_id": "gen-" + release.digest.removeprefix("sha256:")[:24],
            "release_digest": release.digest,
            "build_digest": release.build_digest,
            "platform_version": release.platform_version,
            "api_image": release.control.api_image.reference,
            "worker_image": release.control.worker_image.reference,
            "migration_revision": release.database.expand_revision,
            "previous_generation": previous,
            "required_bytes": _required_bytes(release),
        }
        return ControlGenerationPlan(
            **content,
            plan_digest="sha256:" + hashlib.sha256(_canonical(content)).hexdigest(),
        )

    def active_generation(self) -> str | None:
        return _active_generation(self._state_root)

    def apply(
        self,
        plan: ControlGenerationPlan,
        release: PlatformRelease,
    ) -> ControlGenerationResult:
        if plan != self.plan(release):
            raise UpgradeConflict("upgrade plan is stale or does not match the release")
        if self._boundary.control_is_running():
            raise UpgradeConflict("control plane is running; stop API and worker first")
        available = self._boundary.available_bytes()
        if isinstance(available, bool) or not isinstance(available, int) or available < plan.required_bytes:
            raise UpgradeConflict("insufficient disk space for control generation")

        _secure_directory(self._state_root)
        generations = self._state_root / "generations"
        staging = generations / f".{plan.generation_id}.staging"
        final = generations / plan.generation_id
        environment = {
            "CONTROL_API_IMAGE": plan.api_image,
            "CONTROL_WORKER_IMAGE": plan.worker_image,
            "DGX_PLATFORM_BUILD_DIGEST": plan.build_digest,
            "DGX_PLATFORM_RELEASE_DIGEST": plan.release_digest,
            "DGX_PLATFORM_VERSION": plan.platform_version,
        }
        references = (plan.api_image, plan.worker_image)
        offline_lock: int | None = None
        try:
            try:
                offline_lock = _acquire_offline_lock(self._state_root / "offline.lock")
                _secure_directory(generations)
                if (
                    staging.exists()
                    or staging.is_symlink()
                    or final.exists()
                    or final.is_symlink()
                ):
                    raise UpgradeConflict("control generation already exists")
                staging.mkdir(mode=0o700)
                self._boundary.pull(references)
                rendered = self._boundary.render_compose(environment)
                if (
                    not isinstance(rendered, bytes)
                    or not 0 < len(rendered) <= _MAX_RENDERED_COMPOSE
                ):
                    raise UpgradeError("rendered Compose generation is invalid")
                _write_new(staging / "compose.rendered.yaml", rendered)
                _write_new(
                    staging / "platform.env",
                    "".join(
                        f"{key}={environment[key]}\n" for key in sorted(environment)
                    ).encode(),
                )
                backup = self._boundary.backup(plan.generation_id)
                _json_mapping(backup, "backup manifest")
                self._boundary.stop_worker()
                try:
                    self._boundary.migrate(plan.migration_revision)
                except AmbiguousMigrationError as error:
                    recovery = {
                        "schema_version": 1,
                        "generation_id": plan.generation_id,
                        "previous_generation": plan.previous_generation,
                        "phase": "migration-ambiguous",
                        "release_digest": plan.release_digest,
                    }
                    _write_atomic(
                        self._state_root / "recovery-required.json",
                        _canonical(recovery),
                    )
                    raise UpgradeRecoveryRequired(
                        "database migration is ambiguous; operator recovery is required"
                    ) from error
            finally:
                if offline_lock is not None:
                    _release_offline_lock(offline_lock)
            self._boundary.start_api(staging)
            try:
                readiness = self._boundary.readiness()
                _json_mapping(readiness, "readiness evidence")
            except UpgradeReadinessError:
                self._boundary.stop_api()
                if plan.previous_generation is None:
                    raise UpgradeRecoveryRequired(
                        "candidate failed readiness and no prior generation exists"
                    )
                previous_path = _generation_path(generations, plan.previous_generation)
                self._boundary.restore_generation(previous_path)
                raise
            self._boundary.start_worker()
            receipt = {
                "schema_version": 1,
                "generation_id": plan.generation_id,
                "release_digest": plan.release_digest,
                "build_digest": plan.build_digest,
                "platform_version": plan.platform_version,
                "api_image": plan.api_image,
                "worker_image": plan.worker_image,
                "migration_revision": plan.migration_revision,
                "previous_generation": plan.previous_generation,
                "compose_sha256": hashlib.sha256(rendered).hexdigest(),
                "backup": backup,
                "readiness": readiness,
                "status": "active",
            }
            _write_new(staging / "generation.json", _canonical(receipt))
            os.replace(staging, final)
            _fsync_directory(generations)
            _write_atomic(
                self._state_root / "active-generation",
                (plan.generation_id + "\n").encode(),
            )
            return ControlGenerationResult(
                generation_id=plan.generation_id,
                release_digest=plan.release_digest,
                build_digest=plan.build_digest,
                previous_generation=plan.previous_generation,
                status="active",
            )
        except (UpgradeError, OSError):
            raise
        except Exception as error:
            raise UpgradeError("control generation upgrade failed") from error

    def rollback(self, generation_id: str) -> ControlGenerationResult:
        if self._boundary.control_is_running():
            raise UpgradeConflict("control plane is running; stop API and worker first")
        active = _active_generation(self._state_root)
        if active is None:
            raise UpgradeConflict("there is no active control generation")
        generations = self._state_root / "generations"
        active_receipt = _generation_receipt(_generation_path(generations, active))
        if active_receipt.get("previous_generation") != generation_id:
            raise UpgradeConflict("rollback target is not the recorded predecessor")
        target_path = _generation_path(generations, generation_id)
        target_receipt = _generation_receipt(target_path)
        self._boundary.restore_generation(target_path)
        _write_atomic(
            self._state_root / "active-generation",
            (generation_id + "\n").encode(),
        )
        evidence = {
            "schema_version": 1,
            "from_generation": active,
            "to_generation": generation_id,
            "from_release_digest": active_receipt.get("release_digest"),
            "to_release_digest": target_receipt.get("release_digest"),
            "status": "rolled-back",
        }
        _write_atomic(
            self._state_root / f"rollback-{active}.json",
            _canonical(evidence),
        )
        return ControlGenerationResult(
            generation_id=generation_id,
            release_digest=_receipt_string(target_receipt, "release_digest"),
            build_digest=_receipt_string(target_receipt, "build_digest"),
            previous_generation=active,
            status="rolled-back",
        )


def _required_bytes(release: PlatformRelease) -> int:
    artifacts = (
        release.control.api_image,
        release.control.worker_image,
        *release.control.assets,
    )
    return sum(item.size for item in artifacts) + 1024 * 1024


def _active_generation(state_root: Path) -> str | None:
    path = state_root / "active-generation"
    try:
        if path.is_symlink():
            raise UpgradeConflict("active generation marker is unsafe")
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise UpgradeConflict("active generation marker is unreadable") from error
    if _GENERATION.fullmatch(value) is None:
        raise UpgradeConflict("active generation marker is invalid")
    return value


def _generation_path(generations: Path, generation_id: str) -> Path:
    if _GENERATION.fullmatch(generation_id) is None:
        raise UpgradeConflict("generation ID is invalid")
    path = generations / generation_id
    if path.is_symlink() or not path.is_dir():
        raise UpgradeRecoveryRequired("previous generation is unavailable")
    return path


def _generation_receipt(generation: Path) -> dict[str, object]:
    path = generation / "generation.json"
    try:
        if path.is_symlink():
            raise UpgradeConflict("generation receipt is unsafe")
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise UpgradeConflict("generation receipt is unreadable") from error
    if not isinstance(value, dict) or value.get("generation_id") != generation.name:
        raise UpgradeConflict("generation receipt is invalid")
    return value


def _receipt_string(receipt: dict[str, object], name: str) -> str:
    value = receipt.get(name)
    if not isinstance(value, str):
        raise UpgradeConflict(f"generation receipt {name} is invalid")
    return value


def _secure_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        metadata = path.lstat()
    except OSError as error:
        raise UpgradeConflict("upgrade state directory is unsafe") from error
    if not path.is_dir() or path.is_symlink() or metadata.st_uid not in {0, os.geteuid()}:
        raise UpgradeConflict("upgrade state directory is unsafe")
    os.chmod(path, 0o700)


def _acquire_offline_lock(path: Path) -> int:
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as error:
        raise UpgradeConflict("offline maintenance lock is unsafe") from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(descriptor)
        raise UpgradeConflict("another offline maintenance lock is active") from error
    return descriptor


def _release_offline_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _write_new(path: Path, content: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        _write_all(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_atomic(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.new")
    try:
        _write_new(temporary, content)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise UpgradeError("control generation write was incomplete")
        offset += written


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _json_mapping(value: object, label: str) -> None:
    if not isinstance(value, dict) or not value:
        raise UpgradeError(f"{label} is invalid")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise UpgradeError(f"{label} is invalid") from error
