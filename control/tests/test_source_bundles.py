from __future__ import annotations

import io
import tarfile

import pytest
from vonk_control.source_bundles import (
    BundleLimits,
    SourceBundleError,
    SourceBundleStore,
    inspect_source_bundle,
)

LIMITS = BundleLimits(
    max_archive_bytes=16_384,
    max_files=8,
    max_file_bytes=4096,
    max_total_bytes=8192,
)


def archive(files: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as bundle:
        for name, content in files:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            bundle.addfile(info, io.BytesIO(content))
    return output.getvalue()


@pytest.mark.parametrize("name", ["/etc/passwd", "../escape", "a/../../escape"])
def test_bundle_rejects_paths_outside_context(name: str) -> None:
    with pytest.raises(SourceBundleError) as error:
        inspect_source_bundle(io.BytesIO(archive([(name, b"x")])), LIMITS)

    assert error.value.code == "bundle.path_forbidden"


def test_bundle_digest_is_archive_order_independent() -> None:
    first = inspect_source_bundle(
        io.BytesIO(archive([("Dockerfile", b"FROM scratch\n"), ("x", b"1")])),
        LIMITS,
    )
    second = inspect_source_bundle(
        io.BytesIO(archive([("x", b"1"), ("Dockerfile", b"FROM scratch\n")])),
        LIMITS,
    )

    assert first.sha256 == second.sha256
    assert [item.path for item in first.files] == ["Dockerfile", "x"]


def test_bundle_rejects_links_and_expansion_overflow() -> None:
    linked = io.BytesIO()
    with tarfile.open(fileobj=linked, mode="w") as bundle:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        bundle.addfile(info)
    with pytest.raises(SourceBundleError, match="regular files"):
        inspect_source_bundle(io.BytesIO(linked.getvalue()), LIMITS)

    with pytest.raises(SourceBundleError) as error:
        inspect_source_bundle(io.BytesIO(archive([("large", b"x" * 4097)])), LIMITS)
    assert error.value.code == "bundle.file_too_large"


def test_store_is_content_addressed_and_idempotent(tmp_path) -> None:
    payload = archive([("Dockerfile", b"FROM scratch\n")])
    manifest = inspect_source_bundle(io.BytesIO(payload), LIMITS)
    store = SourceBundleStore(tmp_path, limits=LIMITS)

    first = store.put(manifest.sha256, io.BytesIO(payload))
    second = store.put(manifest.sha256, io.BytesIO(payload))
    loaded = store.get(manifest.sha256)

    assert first == second
    assert first.path.read_bytes() == payload
    assert loaded.archive == payload
    assert loaded.files["Dockerfile"] == b"FROM scratch\n"


def test_store_rejects_expected_digest_mismatch(tmp_path) -> None:
    store = SourceBundleStore(tmp_path, limits=LIMITS)
    with pytest.raises(SourceBundleError) as error:
        store.put("f" * 64, io.BytesIO(archive([("Dockerfile", b"FROM scratch\n")])))

    assert error.value.code == "bundle.digest_mismatch"
