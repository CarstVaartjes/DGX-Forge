"""Generation-bound authenticated reconnect readiness publication."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from collections.abc import Mapping
from pathlib import Path

RUNTIME_ROOT = Path("/run/vonk-forge-agent")
_ENVIRONMENT = {
    "generation": "VONK_AGENT_SUPERVISOR_GENERATION",
    "slot": "VONK_AGENT_SUPERVISOR_SLOT",
    "sha256": "VONK_AGENT_SUPERVISOR_SHA256",
}
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CHALLENGE_NAME = "activation-challenge"


class ReadinessError(RuntimeError):
    """Supervisor readiness context or destination is unsafe."""


class ReadinessReporter:
    def __init__(self, marker: dict[str, object] | None, runtime_root: Path) -> None:
        self._marker = marker
        self._runtime_root = Path(runtime_root)

    @classmethod
    def from_environment(cls) -> ReadinessReporter:
        return cls._parse(os.environ, RUNTIME_ROOT)

    @classmethod
    def _from_environment_for_test(
        cls, environment: Mapping[str, str], runtime_root: Path
    ) -> ReadinessReporter:
        return cls._parse(environment, Path(runtime_root))

    @classmethod
    def _parse(
        cls, environment: Mapping[str, str], runtime_root: Path
    ) -> ReadinessReporter:
        values = {field: environment.get(name) for field, name in _ENVIRONMENT.items()}
        present = {field for field, value in values.items() if value is not None}
        if not present:
            return cls(None, runtime_root)
        if present != set(_ENVIRONMENT):
            raise ReadinessError("supervisor readiness environment is incomplete")
        generation = values["generation"]
        slot = values["slot"]
        digest = values["sha256"]
        if (
            generation is None
            or not re.fullmatch(r"[1-9][0-9]{0,8}", generation)
            or slot not in {"A", "B"}
            or digest is None
            or not _DIGEST.fullmatch(digest)
        ):
            raise ReadinessError("supervisor readiness environment is invalid")
        challenge = _read_challenge_credential(environment)
        return cls(
            {
                "challenge": challenge,
                "generation": int(generation),
                "pid": os.getpid(),
                "schema_version": 2,
                "sha256": digest,
                "slot": slot,
            },
            runtime_root,
        )

    def report(self) -> bool:
        if self._marker is None:
            return False
        directory = _open_runtime(self._runtime_root)
        temporary = f".readiness.{secrets.token_hex(8)}.new"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory,
            )
            raw = (
                json.dumps(self._marker, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("ascii")
            offset = 0
            while offset < len(raw):
                offset += os.write(descriptor, raw[offset:])
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise ReadinessError("readiness staging file is unsafe")
            os.close(descriptor)
            descriptor = -1
            os.rename(
                temporary, "readiness.json", src_dir_fd=directory, dst_dir_fd=directory
            )
            os.fsync(directory)
            self._marker = None
            return True
        except ReadinessError:
            raise
        except OSError as error:
            raise ReadinessError("readiness marker cannot be published") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)


def _open_runtime(path: Path) -> int:
    if not path.is_absolute() or len(path.parts) < 2:
        raise ReadinessError("readiness runtime path is invalid")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, component in enumerate(path.parts[1:]):
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
            metadata = os.fstat(descriptor)
            mode = stat.S_IMODE(metadata.st_mode)
            final = index == len(path.parts[1:]) - 1
            if final:
                if metadata.st_uid != os.geteuid() or mode != 0o700:
                    raise ReadinessError("readiness runtime directory is unsafe")
            elif metadata.st_uid not in {0, os.geteuid()} or (
                mode & 0o022 and not mode & stat.S_ISVTX
            ):
                raise ReadinessError("readiness path ancestry is unsafe")
        return descriptor
    except ReadinessError:
        os.close(descriptor)
        raise
    except OSError as error:
        os.close(descriptor)
        raise ReadinessError("readiness runtime directory is unavailable") from error


def _read_challenge_credential(environment: Mapping[str, str]) -> str:
    directory_value = environment.get("CREDENTIALS_DIRECTORY")
    if directory_value is None:
        raise ReadinessError("activation challenge credential is unavailable")
    directory_path = Path(directory_value)
    if not directory_path.is_absolute():
        raise ReadinessError("activation challenge credential path is invalid")
    directory = descriptor = -1
    try:
        directory = os.open(
            directory_path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        descriptor = os.open(
            _CHALLENGE_NAME,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ReadinessError("activation challenge credential is unsafe")
        raw = os.read(descriptor, 66)
        if len(raw) != 65 or raw[-1:] != b"\n":
            raise ReadinessError("activation challenge credential is invalid")
        challenge = raw[:-1].decode("ascii")
        if not _DIGEST.fullmatch(challenge):
            raise ReadinessError("activation challenge credential is invalid")
        return challenge
    except ReadinessError:
        raise
    except (OSError, UnicodeDecodeError) as error:
        raise ReadinessError("activation challenge credential is unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory >= 0:
            os.close(directory)
