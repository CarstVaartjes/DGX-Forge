"""Resumable, journal-backed acquisition of immutable workload components."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Protocol

from .providers import (
    ComponentDescriptor,
    FetchResponse,
    ProviderError,
    ProviderRegistry,
    Validators,
)


class AcquisitionError(RuntimeError):
    """An immutable component could not be acquired safely."""


class AcquisitionCancelled(AcquisitionError):
    """The caller durably cancelled component acquisition."""


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    bytes_reserved: int


@dataclass(frozen=True)
class DownloadRecord:
    component: str
    digest: str
    expected_size: int
    bytes_completed: int
    validators: Validators


@dataclass(frozen=True)
class StoreObject:
    digest: str
    size: int
    kind: str
    relative_name: str


class AcquisitionStore(Protocol):
    def lookup(self, digest: str, size: int) -> StoreObject | None: ...

    def reserve(self, binding: object, bytes_required: int) -> Reservation: ...

    def release(self, reservation: Reservation) -> None: ...

    def begin_component(
        self, reservation: Reservation, descriptor: ComponentDescriptor
    ) -> DownloadRecord: ...

    def iter_partial(self, record: DownloadRecord) -> Iterable[bytes]: ...

    def append_partial(self, record: DownloadRecord, chunk: bytes) -> None: ...

    def checkpoint(
        self,
        record: DownloadRecord,
        bytes_completed: int,
        validators: Validators,
    ) -> None: ...

    def reset_partial(self, record: DownloadRecord, reason: str) -> DownloadRecord: ...

    def quarantine_partial(
        self, record: DownloadRecord, reason: str
    ) -> DownloadRecord: ...

    def pause(self, record: DownloadRecord) -> None: ...

    def promote_component(
        self, record: DownloadRecord, verified_digest: str
    ) -> StoreObject: ...


ProgressCallback = Callable[[Mapping[str, object]], None]
CancellationProbe = Callable[[], bool]


def _deadline_check(deadline: object | None) -> None:
    if deadline is not None and hasattr(deadline, "check"):
        try:
            deadline.check()
        except Exception as error:
            raise AcquisitionError("acquisition deadline elapsed") from error


def _progress(
    callback: ProgressCallback,
    descriptor: ComponentDescriptor,
    *,
    completed: int,
    cache_hits: int,
    objects_completed: int,
    reserved_bytes: int,
) -> None:
    callback(
        {
            "phase": "fetch",
            "component": descriptor.name,
            "bytes_completed": completed,
            "bytes_total": descriptor.size,
            "objects_completed": objects_completed,
            "objects_total": 1,
            "cache_hits": cache_hits,
            "reserved_bytes": reserved_bytes,
        }
    )


def _prefix_hash(
    store: AcquisitionStore,
    record: DownloadRecord,
) -> tuple[hashlib._Hash, int]:
    digest = hashlib.sha256()
    observed = 0
    for chunk in store.iter_partial(record):
        if not isinstance(chunk, bytes) or not chunk:
            raise AcquisitionError("partial component journal is invalid")
        observed += len(chunk)
        if observed > record.expected_size:
            raise AcquisitionError("partial component exceeds its size bound")
        digest.update(chunk)
    if observed != record.bytes_completed:
        raise AcquisitionError("partial component journal is inconsistent")
    return digest, observed


class AcquisitionEngine:
    """Acquire one descriptor through ordered mirrors and one durable journal."""

    def __init__(
        self,
        store: AcquisitionStore,
        providers: ProviderRegistry,
        *,
        chunk_limit: int = 4 * 1024 * 1024,
    ) -> None:
        if (
            isinstance(chunk_limit, bool)
            or not isinstance(chunk_limit, int)
            or not 1 <= chunk_limit <= 16 * 1024 * 1024
        ):
            raise ValueError("acquisition chunk limit is invalid")
        self._store = store
        self._providers = providers
        self._chunk_limit = chunk_limit

    def fetch(
        self,
        descriptor: ComponentDescriptor,
        binding: object,
        progress: ProgressCallback,
        cancelled: CancellationProbe,
        *,
        deadline: object | None = None,
    ) -> StoreObject:
        if not isinstance(descriptor, ComponentDescriptor):
            raise TypeError("component descriptor is invalid")
        if not callable(progress) or not callable(cancelled):
            raise TypeError("acquisition callbacks must be callable")
        expected_digest = descriptor.digest.removeprefix("sha256:")
        cached = self._store.lookup(expected_digest, descriptor.size)
        if cached is not None:
            _progress(
                progress,
                descriptor,
                completed=descriptor.size,
                cache_hits=1,
                objects_completed=1,
                reserved_bytes=0,
            )
            return cached
        if cancelled():
            raise AcquisitionCancelled(
                f"component acquisition cancelled: {descriptor.name}"
            )
        _deadline_check(deadline)
        reserve_component = getattr(self._store, "reserve_component", None)
        reservation = (
            reserve_component(binding, descriptor)
            if callable(reserve_component)
            else self._store.reserve(binding, descriptor.size)
        )
        record = self._store.begin_component(reservation, descriptor)
        if (
            getattr(record, "component", None) != descriptor.name
            or getattr(record, "digest", None) != expected_digest
            or getattr(record, "expected_size", None) != descriptor.size
            or type(getattr(record, "bytes_completed", None)) is not int
            or not 0 <= record.bytes_completed <= descriptor.size
        ):
            self._store.release(reservation)
            raise AcquisitionError("component download journal is invalid")
        record_state = getattr(record, "state", "partial")
        if record_state == "complete":
            cached = self._store.lookup(expected_digest, descriptor.size)
            if cached is None:
                self._store.release(reservation)
                raise AcquisitionError("completed component is unavailable")
            _progress(
                progress,
                descriptor,
                completed=descriptor.size,
                cache_hits=1,
                objects_completed=1,
                reserved_bytes=reservation.bytes_reserved,
            )
            self._store.release(reservation)
            return cached
        if record_state == "shared":
            waiter = getattr(self._store, "wait_for_component", None)
            if not callable(waiter):
                self._store.release(reservation)
                raise AcquisitionError("component acquisition is already in progress")
            cached = waiter(
                binding,
                expected_digest,
                descriptor.size,
                cancelled,
                deadline=deadline,
            )
            self._store.release(reservation)
            if cached is None:
                raise AcquisitionError(
                    f"component acquisition owner did not complete: {descriptor.name}"
                )
            _progress(
                progress,
                descriptor,
                completed=descriptor.size,
                cache_hits=1,
                objects_completed=1,
                reserved_bytes=reservation.bytes_reserved,
            )
            return cached
        _progress(
            progress,
            descriptor,
            completed=record.bytes_completed,
            cache_hits=0,
            objects_completed=0,
            reserved_bytes=reservation.bytes_reserved,
        )
        last_reason = "source-unavailable"
        try:
            for source in descriptor.sources:
                # A failed mirror may have durably advanced or reset the partial.
                # Reload it before choosing the offset for the next mirror.
                record = self._store.begin_component(reservation, descriptor)
                if (
                    getattr(record, "component", None) != descriptor.name
                    or getattr(record, "digest", None) != expected_digest
                    or getattr(record, "expected_size", None) != descriptor.size
                    or type(getattr(record, "bytes_completed", None)) is not int
                    or not 0 <= record.bytes_completed <= descriptor.size
                ):
                    raise AcquisitionError("component download journal is invalid")
                try:
                    result = self._from_source(
                        descriptor,
                        source,
                        record,
                        reservation,
                        progress,
                        cancelled,
                        deadline,
                    )
                except AcquisitionCancelled:
                    raise
                except (AcquisitionError, ProviderError) as error:
                    last_reason = getattr(error, "reason", "source-rejected")
                    continue
                return result
        finally:
            self._store.release(reservation)
        raise AcquisitionError(
            f"component acquisition failed: {descriptor.name} ({last_reason})"
        )

    def _from_source(
        self,
        descriptor: ComponentDescriptor,
        source,
        record: DownloadRecord,
        reservation: Reservation,
        progress: ProgressCallback,
        cancelled: CancellationProbe,
        deadline: object | None,
    ) -> StoreObject:
        provider = self._providers.provider(source.provider)
        digest, completed = _prefix_hash(self._store, record)
        response = self._open(
            provider,
            source,
            completed,
            record.validators,
            deadline,
        )
        if completed and (
            response.start_offset != completed
            or not record.validators.compatible_with(response.validators)
        ):
            record = self._store.reset_partial(record, "resume-validator-mismatch")
            digest = hashlib.sha256()
            completed = 0
            response = self._open(provider, source, 0, Validators(), deadline)
        if response.start_offset != completed:
            raise AcquisitionError("source range response is invalid")
        if response.total_size is not None and response.total_size != descriptor.size:
            self._store.quarantine_partial(record, "declared-size-mismatch")
            raise AcquisitionError("source declared size is invalid")
        self._store.checkpoint(record, completed, response.validators)
        record = replace(
            record, bytes_completed=completed, validators=response.validators
        )
        try:
            for chunk in response.chunks:
                _deadline_check(deadline)
                if cancelled():
                    self._store.pause(record)
                    raise AcquisitionCancelled(
                        f"component acquisition cancelled: {descriptor.name}"
                    )
                if (
                    not isinstance(chunk, bytes)
                    or not chunk
                    or len(chunk) > self._chunk_limit
                ):
                    raise AcquisitionError("source chunk is invalid")
                if completed + len(chunk) > descriptor.size:
                    self._store.quarantine_partial(record, "observed-size-overflow")
                    raise AcquisitionError("source exceeded declared size")
                self._store.append_partial(record, chunk)
                digest.update(chunk)
                completed += len(chunk)
                self._store.checkpoint(record, completed, response.validators)
                record = replace(record, bytes_completed=completed)
                _progress(
                    progress,
                    descriptor,
                    completed=completed,
                    cache_hits=0,
                    objects_completed=0,
                    reserved_bytes=reservation.bytes_reserved,
                )
        except AcquisitionCancelled:
            raise
        except AcquisitionError:
            raise
        except Exception as error:
            raise AcquisitionError("source stream failed") from error
        if cancelled():
            self._store.pause(record)
            raise AcquisitionCancelled(
                f"component acquisition cancelled: {descriptor.name}"
            )
        if completed != descriptor.size:
            raise AcquisitionError("source ended before declared size")
        observed_digest = digest.hexdigest()
        if observed_digest != descriptor.digest.removeprefix("sha256:"):
            self._store.quarantine_partial(record, "digest-mismatch")
            raise AcquisitionError("source content digest is invalid")
        result = self._store.promote_component(record, observed_digest)
        if (
            getattr(result, "digest", None) != observed_digest
            or getattr(result, "size", None) != descriptor.size
            or not isinstance(getattr(result, "kind", None), str)
            or not isinstance(getattr(result, "relative_name", None), str)
        ):
            raise AcquisitionError("store promotion receipt is invalid")
        _progress(
            progress,
            descriptor,
            completed=descriptor.size,
            cache_hits=0,
            objects_completed=1,
            reserved_bytes=reservation.bytes_reserved,
        )
        return result

    @staticmethod
    def _open(provider, source, offset, validators, deadline) -> FetchResponse:
        response = provider.open(source, offset, validators, deadline)
        if not isinstance(response, FetchResponse):
            raise AcquisitionError("source provider response is invalid")
        return response
