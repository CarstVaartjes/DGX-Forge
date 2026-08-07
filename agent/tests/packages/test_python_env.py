from __future__ import annotations

import hashlib
import io
import stat
import sysconfig
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from vonk_agent.package_operations import OperationBinding
from vonk_agent.packages.python_env import (
    PythonEnvironmentBuilder,
    PythonEnvironmentCancelled,
    PythonEnvironmentError,
    PythonEnvironmentSpec,
    PythonRuntimeIdentity,
)
from vonk_agent.packages.store import ComponentDescriptor, ContentStore


@dataclass(frozen=True)
class StoredObject:
    digest: str
    size: int
    kind: str
    relative_name: str


class ObjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "objects").mkdir(parents=True, exist_ok=True)
        (root / "quarantine").mkdir(exist_ok=True)
        self.derived: dict[str, str] = {}
        self.publications = 0

    def add(self, content: bytes, *, kind: str) -> StoredObject:
        digest = hashlib.sha256(content).hexdigest()
        path = self.root / "objects" / digest
        path.write_bytes(content)
        path.chmod(0o444)
        return StoredObject(digest, len(content), kind, f"objects/{digest}")

    def object_path(self, value: StoredObject) -> Path:
        return self.root / value.relative_name

    def lookup(self, digest: str) -> StoredObject | None:
        path = self.root / "objects" / digest
        if not path.exists():
            return None
        return StoredObject(
            digest,
            path.stat().st_size,
            "python-environment",
            f"objects/{digest}",
        )

    def lookup_derived(self, derivation_digest: str) -> StoredObject | None:
        digest = self.derived.get(derivation_digest)
        if digest is None:
            return None
        path = self.root / "objects" / digest
        if not path.exists():
            return None
        return StoredObject(
            digest, path.stat().st_size, "python-environment", f"objects/{digest}"
        )

    def publish_derived(
        self,
        binding: object,
        *,
        derivation_digest: str,
        content: bytes,
        kind: str,
    ) -> StoredObject:
        del binding
        value = self.add(content, kind=kind)
        self.derived[derivation_digest] = value.digest
        self.publications += 1
        return value

    def is_immutable(self, value: StoredObject) -> bool:
        path = self.object_path(value)
        return (
            path.is_file()
            and path.stat().st_nlink == 1
            and stat.S_IMODE(path.stat().st_mode) == 0o444
            and hashlib.sha256(path.read_bytes()).hexdigest() == value.digest
        )

    def quarantine(self, value: StoredObject, binding: object) -> None:
        del binding
        source = self.object_path(value)
        target = (
            self.root
            / "quarantine"
            / f"{value.digest}-{len(tuple((self.root / 'quarantine').iterdir()))}"
        )
        source.replace(target)


class SourceBuildSandbox:
    def __init__(self, wheel: bytes = b"", *, fail_imports: bool = False) -> None:
        self.wheel = wheel
        self.calls: list[dict[str, object]] = []
        self.validation_calls: list[dict[str, object]] = []
        self.fail_imports = fail_imports

    def build_wheel(
        self,
        source: Path,
        *,
        network: bool,
        devices: tuple[str, ...],
        host_mounts: tuple[str, ...],
        build_identity: str,
        cancelled,
        deadline: object | None,
    ) -> bytes:
        assert source.is_file()
        assert cancelled() is False
        self.calls.append(
            {
                "build_identity": build_identity,
                "deadline": deadline,
                "devices": devices,
                "host_mounts": host_mounts,
                "network": network,
            }
        )
        return self.wheel

    def validate_imports(
        self,
        environment: Path,
        imports: tuple[str, ...],
        *,
        network: bool,
        devices: tuple[str, ...],
        host_mounts: tuple[str, ...],
        build_identity: str,
        cancelled,
        deadline: object | None,
    ) -> None:
        assert environment.is_file()
        assert cancelled() is False
        self.validation_calls.append(
            {
                "build_identity": build_identity,
                "deadline": deadline,
                "devices": devices,
                "host_mounts": host_mounts,
                "imports": imports,
                "network": network,
            }
        )
        if self.fail_imports:
            raise RuntimeError("sandbox import failed")


class CoordinatedValidationSandbox(SourceBuildSandbox):
    def __init__(self) -> None:
        super().__init__()
        self.first_started = threading.Event()
        self.second_started = threading.Event()
        self.release_first = threading.Event()

    def validate_imports(self, environment: Path, imports: tuple[str, ...], **policy) -> None:
        call = len(self.validation_calls)
        if call == 0:
            self.first_started.set()
            assert self.release_first.wait(2)
            assert environment.is_file()
        else:
            self.second_started.set()
            self.release_first.set()
        super().validate_imports(environment, imports, **policy)


def _wheel(package: str = "demo", *, module: str | None = None) -> bytes:
    output = io.BytesIO()
    module = module or package.replace("-", "_")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(f"{module}/__init__.py", "VALUE = 1\n")
        archive.writestr(
            f"{package}-1.0.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {package}\nVersion: 1.0\n",
        )
        archive.writestr(f"{package}-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\n")
    return output.getvalue()


def _pylock(name: str, wheel_digest: str, *, include_hash: bool = True) -> bytes:
    hash_line = f'    sha256 = "{wheel_digest}"\n' if include_hash else ""
    return (
        'lock-version = "1.0"\n'
        'requires-python = ">=3.12"\n'
        "[[packages]]\n"
        f'name = "{name}"\n'
        'version = "1.0"\n'
        "[[packages.wheels]]\n"
        f'name = "{name}-1.0-py3-none-any.whl"\n'
        "[packages.wheels.hashes]\n"
        f"{hash_line}"
    ).encode()


def _spec(
    lock: StoredObject,
    wheels: tuple[StoredObject, ...],
    *,
    interpreter_digest: str | None = None,
    platform: str | None = None,
    imports: tuple[str, ...] = ("demo",),
    source_builds: tuple[dict[str, object], ...] = (),
) -> PythonEnvironmentSpec:
    runtime = PythonRuntimeIdentity.local()
    return PythonEnvironmentSpec.parse(
        {
            "schema_version": 1,
            "interpreter_digest": f"sha256:{interpreter_digest or runtime.interpreter_digest}",
            "platform": platform or runtime.platform,
            "lock_digest": f"sha256:{lock.digest}",
            "wheel_digests": [f"sha256:{item.digest}" for item in wheels],
            "source_builds": list(source_builds),
            "build_recipe": {
                "schema_version": 1,
                "build_identity": "vonk-workload-build",
                "network": False,
            },
            "imports": list(imports),
        }
    )


def _binding() -> SimpleNamespace:
    return SimpleNamespace(
        job_id="11111111-1111-4111-8111-111111111111",
        operation_id="22222222-2222-4222-8222-222222222222",
        attempt=1,
        fence="33333333-3333-4333-8333-333333333333",
        node_id="spk_0123456789abcdef0123456789abcdef",
    )


def _builder(
    store,
    *,
    sandbox: SourceBuildSandbox | None = None,
    cancelled=None,
    deadline: object | None = None,
) -> PythonEnvironmentBuilder:
    return PythonEnvironmentBuilder(
        store,
        sandbox=sandbox or SourceBuildSandbox(),
        cancelled=cancelled,
        deadline=deadline,
    )


def _real_binding(index: int) -> OperationBinding:
    return OperationBinding(
        job_id=f"00000000-0000-4000-8000-{index:012d}",
        operation_id=f"10000000-0000-4000-8000-{index:012d}",
        attempt=1,
        fence=f"20000000-0000-4000-8000-{index:012d}",
        node_id="spk_0123456789abcdef0123456789abcdef",
    )


def _add_real(
    store: ContentStore,
    binding: OperationBinding,
    content: bytes,
    kind: str,
):
    digest = hashlib.sha256(content).hexdigest()
    reservation = store.reserve(binding, bytes_required=len(content))
    record = store.begin_component(
        reservation,
        ComponentDescriptor(digest, len(content), kind),
    )
    if record.state == "partial":
        record = store.write_partial(record, content)
    result = store.promote_component(record, digest)
    store.release_reservation(reservation)
    return result


def test_runtime_identity_is_validated_and_immutable() -> None:
    runtime = PythonRuntimeIdentity.local()

    assert len(runtime.interpreter_digest) == 64
    assert runtime.interpreter_digest == runtime.interpreter_digest.lower()
    assert sysconfig.get_platform() in runtime.platform

    with pytest.raises(ValueError, match="runtime identity"):
        PythonRuntimeIdentity("not-a-digest", runtime.platform)
    with pytest.raises(ValueError, match="runtime identity"):
        PythonRuntimeIdentity(runtime.interpreter_digest, "not a platform")


def test_runtime_identity_is_exported_from_package_namespace() -> None:
    from vonk_agent.packages import PythonRuntimeIdentity as ExportedIdentity

    assert ExportedIdentity is PythonRuntimeIdentity


def test_wheel_expanded_size_is_checked_before_member_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vonk_agent.packages import python_env

    wheel_bytes = _wheel()
    wheel = zipfile.ZipFile(io.BytesIO(wheel_bytes))
    package_info = next(
        info for info in wheel.infolist() if info.filename.endswith("__init__.py")
    )
    wheel.close()
    monkeypatch.setattr(python_env, "_MAX_ENVIRONMENT_BYTES", 1)

    store = ObjectStore(tmp_path / "store")
    wheel_object = store.add(wheel_bytes, kind="wheel")
    lock = store.add(_pylock("demo", wheel_object.digest), kind="pylock")
    original_read = zipfile.ZipFile.read

    def read(archive: zipfile.ZipFile, member, *args, **kwargs):
        if getattr(member, "filename", member) == package_info.filename:
            raise AssertionError("oversized wheel member was read")
        return original_read(archive, member, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", read)
    with pytest.raises(PythonEnvironmentError, match="exceeds"):
        _builder(store).build(
            _spec(lock, (wheel_object,)),
            {lock.digest: lock, wheel_object.digest: wheel_object},
            _binding(),
        )


def test_failed_environment_publication_releases_capacity_reservation(
    tmp_path: Path,
) -> None:
    store = ContentStore(tmp_path / "store", capacity_bytes=8 * 1024**2)
    wheel = _add_real(store, _real_binding(1), _wheel(), "wheel")
    lock = _add_real(
        store,
        _real_binding(2),
        _pylock("demo", wheel.digest),
        "pylock",
    )

    def fail_promote(*args, **kwargs):
        raise RuntimeError("simulated publication failure")

    store.promote_component = fail_promote  # type: ignore[method-assign]
    with pytest.raises(PythonEnvironmentError, match="publication"):
        _builder(store).build(
            _spec(lock, (wheel,)),
            {lock.digest: lock, wheel.digest: wheel},
            _real_binding(3),
        )

    with store.state.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0


def test_complete_lock_build_is_reproducible_immutable_and_reused(
    tmp_path: Path,
) -> None:
    wheel_bytes = _wheel()
    wheel_digest = hashlib.sha256(wheel_bytes).hexdigest()
    lock_bytes = _pylock("demo", wheel_digest)
    first_store = ObjectStore(tmp_path / "first")
    first_wheel = first_store.add(wheel_bytes, kind="wheel")
    first_lock = first_store.add(lock_bytes, kind="pylock")
    spec = _spec(first_lock, (first_wheel,))
    builder = _builder(first_store)

    first = builder.build(
        spec,
        {
            first_lock.digest: first_lock,
            first_wheel.digest: first_wheel,
        },
        _binding(),
    )
    reused = builder.build(
        spec,
        {
            first_lock.digest: first_lock,
            first_wheel.digest: first_wheel,
        },
        _binding(),
    )

    assert first == reused
    assert first_store.publications == 1
    assert first_store.is_immutable(first)

    second_store = ObjectStore(tmp_path / "second")
    second_wheel = second_store.add(wheel_bytes, kind="wheel")
    second_lock = second_store.add(lock_bytes, kind="pylock")
    rebuilt = _builder(second_store).build(
        _spec(second_lock, (second_wheel,)),
        {second_lock.digest: second_lock, second_wheel.digest: second_wheel},
        _binding(),
    )
    assert rebuilt.digest == first.digest


def test_environment_publishes_and_reuses_through_real_content_store(
    tmp_path: Path,
) -> None:
    root = tmp_path / "packages"
    store = ContentStore(root, capacity_bytes=8 * 1024**2)
    wheel_bytes = _wheel()
    wheel = _add_real(store, _real_binding(1), wheel_bytes, "wheel")
    lock = _add_real(
        store,
        _real_binding(2),
        _pylock("demo", wheel.digest),
        "pylock",
    )
    spec = _spec(lock, (wheel,))

    first = _builder(store).build(
        spec,
        {lock.digest: lock, wheel.digest: wheel},
        _real_binding(3),
    )
    reopened = ContentStore(root, capacity_bytes=8 * 1024**2)
    reused = _builder(reopened).build(
        spec,
        {lock.digest: lock, wheel.digest: wheel},
        _real_binding(4),
    )

    assert reused == first
    assert (
        reopened.object_path(reused).read_bytes()
        == store.object_path(first).read_bytes()
    )


def test_real_content_store_quarantines_mutated_cached_environment(
    tmp_path: Path,
) -> None:
    root = tmp_path / "packages"
    store = ContentStore(root, capacity_bytes=8 * 1024**2)
    wheel = _add_real(store, _real_binding(1), _wheel(), "wheel")
    lock = _add_real(
        store,
        _real_binding(2),
        _pylock("demo", wheel.digest),
        "pylock",
    )
    spec = _spec(lock, (wheel,))
    environment = _builder(store).build(
        spec,
        {lock.digest: lock, wheel.digest: wheel},
        _real_binding(3),
    )
    path = store.object_path(environment)
    path.chmod(0o644)
    path.write_bytes(b"mutated-environment")
    path.chmod(0o444)

    repaired = _builder(store).build(
        spec,
        {lock.digest: lock, wheel.digest: wheel},
        _real_binding(4),
    )

    assert repaired.digest == environment.digest
    assert store.is_immutable(repaired)
    assert len(tuple((root / "quarantine").iterdir())) == 1


def test_interpreter_and_platform_are_part_of_environment_identity(
    tmp_path: Path,
) -> None:
    store = ObjectStore(tmp_path / "store")
    wheel_bytes = _wheel()
    wheel = store.add(wheel_bytes, kind="wheel")
    lock = store.add(_pylock("demo", wheel.digest), kind="pylock")
    objects = {lock.digest: lock, wheel.digest: wheel}
    builder = _builder(store)

    baseline = builder.build(_spec(lock, (wheel,)), objects, _binding())
    interpreter = builder.build(
        _spec(lock, (wheel,), interpreter_digest="2" * 64),
        objects,
        _binding(),
    )
    platform = builder.build(
        _spec(lock, (wheel,), platform="linux-amd64-cp312"),
        objects,
        _binding(),
    )

    assert len({baseline.digest, interpreter.digest, platform.digest}) == 3


def test_mutable_cached_environment_is_quarantined_and_rebuilt(tmp_path: Path) -> None:
    store = ObjectStore(tmp_path / "store")
    wheel = store.add(_wheel(), kind="wheel")
    lock = store.add(_pylock("demo", wheel.digest), kind="pylock")
    spec = _spec(lock, (wheel,))
    objects = {lock.digest: lock, wheel.digest: wheel}
    builder = _builder(store)
    environment = builder.build(spec, objects, _binding())
    store.object_path(environment).chmod(0o644)

    repaired = builder.build(spec, objects, _binding())

    assert repaired.digest == environment.digest
    assert store.is_immutable(repaired)
    assert len(tuple((store.root / "quarantine").iterdir())) == 1
    assert store.publications == 2


@pytest.mark.parametrize("failure", ("live-index", "missing-hash", "unknown"))
def test_rejects_live_resolution_missing_hashes_and_unknown_fields(
    tmp_path: Path,
    failure: str,
) -> None:
    store = ObjectStore(tmp_path / "store")
    wheel = store.add(_wheel(), kind="wheel")
    lock_bytes = _pylock("demo", wheel.digest, include_hash=failure != "missing-hash")
    lock = store.add(lock_bytes, kind="pylock")
    document = {
        "schema_version": 1,
        "interpreter_digest": "sha256:" + "1" * 64,
        "platform": "linux-arm64-cp312",
        "lock_digest": f"sha256:{lock.digest}",
        "wheel_digests": [f"sha256:{wheel.digest}"],
        "source_builds": [],
        "build_recipe": {
            "schema_version": 1,
            "build_identity": "vonk-workload-build",
            "network": False,
        },
        "imports": ["demo"],
    }
    if failure == "live-index":
        document["index_url"] = "https://pypi.org/simple"
    elif failure == "unknown":
        document["command"] = "pip install demo"

    if failure == "missing-hash":
        spec = PythonEnvironmentSpec.parse(document)
        with pytest.raises(PythonEnvironmentError, match="hash"):
            _builder(store).build(
                spec,
                {lock.digest: lock, wheel.digest: wheel},
                _binding(),
            )
    else:
        with pytest.raises(PythonEnvironmentError, match="unknown|index"):
            PythonEnvironmentSpec.parse(document)


def test_source_build_is_digest_bound_and_networkless(tmp_path: Path) -> None:
    built_wheel = _wheel("extension", module="extension")
    built_digest = hashlib.sha256(built_wheel).hexdigest()
    source = b"locked-source-archive"
    store = ObjectStore(tmp_path / "store")
    source_object = store.add(source, kind="python-source")
    lock = store.add(_pylock("extension", built_digest), kind="pylock")
    sandbox = SourceBuildSandbox(built_wheel)
    source_build = {
        "source_digest": f"sha256:{source_object.digest}",
        "wheel_digest": f"sha256:{built_digest}",
    }
    spec = _spec(
        lock,
        (),
        imports=("extension",),
        source_builds=(source_build,),
    )

    result = _builder(store, sandbox=sandbox).build(
        spec,
        {lock.digest: lock, source_object.digest: source_object},
        _binding(),
    )

    assert store.is_immutable(result)
    assert sandbox.calls == [
        {
            "build_identity": "vonk-workload-build",
            "deadline": None,
            "devices": (),
            "host_mounts": (),
            "network": False,
        }
    ]


def test_failed_import_validation_and_cancellation_publish_nothing(
    tmp_path: Path,
) -> None:
    store = ObjectStore(tmp_path / "store")
    wheel = store.add(_wheel(), kind="wheel")
    lock = store.add(_pylock("demo", wheel.digest), kind="pylock")
    objects = {lock.digest: lock, wheel.digest: wheel}

    with pytest.raises(PythonEnvironmentError, match="import"):
        _builder(store).build(
            _spec(lock, (wheel,), imports=("missing_module",)),
            objects,
            _binding(),
        )
    with pytest.raises(PythonEnvironmentCancelled):
        _builder(store, cancelled=lambda binding: True).build(
            _spec(lock, (wheel,)),
            objects,
            _binding(),
        )

    assert store.publications == 0


def test_wheel_metadata_must_match_the_complete_lock(tmp_path: Path) -> None:
    store = ObjectStore(tmp_path / "store")
    wheel = store.add(_wheel("other", module="demo"), kind="wheel")
    lock = store.add(_pylock("demo", wheel.digest), kind="pylock")

    with pytest.raises(PythonEnvironmentError, match="metadata|lock"):
        _builder(store).build(
            _spec(lock, (wheel,)),
            {lock.digest: lock, wheel.digest: wheel},
            _binding(),
        )

    assert store.publications == 0


def test_import_validation_uses_the_networkless_cancellable_sandbox(
    tmp_path: Path,
) -> None:
    store = ObjectStore(tmp_path / "store")
    wheel = store.add(_wheel(), kind="wheel")
    lock = store.add(_pylock("demo", wheel.digest), kind="pylock")
    sandbox = SourceBuildSandbox(fail_imports=True)
    deadline = object()

    with pytest.raises(PythonEnvironmentError, match="import"):
        _builder(store, sandbox=sandbox, deadline=deadline).build(
            _spec(lock, (wheel,)),
            {lock.digest: lock, wheel.digest: wheel},
            _binding(),
        )

    assert sandbox.validation_calls == [
        {
            "build_identity": "vonk-workload-build",
            "deadline": deadline,
            "devices": (),
            "host_mounts": (),
            "imports": ("demo",),
            "network": False,
        }
    ]
    assert store.publications == 0


def test_restart_removes_abandoned_environment_staging(tmp_path: Path) -> None:
    store = ObjectStore(tmp_path / "store")
    wheel = store.add(_wheel(), kind="wheel")
    lock_bytes = _pylock("demo", wheel.digest)
    lock = store.add(lock_bytes, kind="pylock")
    spec = _spec(lock, (wheel,))
    derivation = spec.derivation_digest(lock_bytes)
    stale = (
        store.root
        / "staging"
        / f"python-environment-{derivation}-stale.partial"
    )
    stale.mkdir(parents=True)
    (stale / "untrusted").write_text("stale")

    _builder(store).build(
        spec,
        {lock.digest: lock, wheel.digest: wheel},
        _binding(),
    )

    assert not stale.exists()
    assert not list((store.root / "staging").glob("python-environment-*.partial"))


def test_concurrent_same_environment_derivation_never_deletes_peer_staging(
    tmp_path: Path,
) -> None:
    store = ObjectStore(tmp_path / "store")
    wheel = store.add(_wheel(), kind="wheel")
    lock = store.add(_pylock("demo", wheel.digest), kind="pylock")
    spec = _spec(lock, (wheel,))
    objects = {lock.digest: lock, wheel.digest: wheel}
    sandbox = CoordinatedValidationSandbox()
    results: list[StoredObject] = []
    errors: list[Exception] = []

    def run() -> None:
        try:
            results.append(
                _builder(store, sandbox=sandbox).build(spec, objects, _binding())
            )
        except Exception as error:  # noqa: BLE001 - cross-thread test boundary
            errors.append(error)

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    assert sandbox.first_started.wait(2), errors
    second.start()
    if not sandbox.second_started.wait(0.1):
        sandbox.release_first.set()
    first.join(2)
    second.join(2)

    assert not errors
    assert len(results) == 2
    assert results[0] == results[1]
