from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from vonk_control.host_commands import BoundedCommandRunner, CommandPolicy
from vonk_control.oci_bundle import OciBundleError, OciBundleSource

from cluster_profiles.platform_release import OciDeploymentBundle

MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
LAYER_MEDIA_TYPE = "application/vnd.dgx-forge.control-deployment.v1.tar"


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _manifest(layer: bytes, **layer_changes: object) -> bytes:
    layer_descriptor: dict[str, object] = {
        "mediaType": LAYER_MEDIA_TYPE,
        "digest": _digest(layer),
        "size": len(layer),
    }
    layer_descriptor.update(layer_changes)
    document = {
        "schemaVersion": 2,
        "mediaType": MANIFEST_MEDIA_TYPE,
        "artifactType": "application/vnd.dgx-forge.control-deployment.v1",
        "config": {
            "mediaType": "application/vnd.unknown.config.v1+json",
            "digest": "sha256:" + "0" * 64,
            "size": 2,
        },
        "layers": [layer_descriptor],
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def _descriptor(manifest: bytes, layer: bytes) -> OciDeploymentBundle:
    manifest_digest = _digest(manifest)
    return OciDeploymentBundle(
        reference=(
            "registry.example:5443/dgx-forge/control-deployment@" + manifest_digest
        ),
        manifest_digest=manifest_digest,
        manifest_size=len(manifest),
        manifest_media_type=MANIFEST_MEDIA_TYPE,
        layer_digest=_digest(layer),
        layer_size=len(layer),
        layer_media_type=LAYER_MEDIA_TYPE,
    )


def _fake_oras(tmp_path: Path) -> Path:
    executable = tmp_path / "oras"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys,time\n"
        "args=sys.argv[1:]\n"
        "with open(os.environ['ORAS_LOG'], 'a', encoding='utf-8') as out:\n"
        "    out.write(json.dumps(args, separators=(',', ':')) + '\\n')\n"
        "mode=os.environ.get('ORAS_MODE', '')\n"
        "if mode == 'sleep': time.sleep(30)\n"
        "if mode == 'fail':\n"
        "    sys.stderr.write('registry failed with private-token=' + 'x' * 10000)\n"
        "    raise SystemExit(17)\n"
        "if args[:2] == ['manifest', 'fetch']:\n"
        "    raw=os.environ['ORAS_MANIFEST_FILE']\n"
        "elif args[:2] == ['blob', 'fetch']:\n"
        "    raw=os.environ['ORAS_LAYER_FILE']\n"
        "else:\n"
        "    raise SystemExit(19)\n"
        "sys.stdout.buffer.write(open(raw, 'rb').read())\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _source(
    tmp_path: Path,
    manifest: bytes,
    layer: bytes,
    *,
    mode: str = "",
    timeout: float = 2,
    stderr_limit: int = 1024,
) -> tuple[OciBundleSource, Path]:
    log = tmp_path / "oras.log"
    manifest_file = tmp_path / "manifest.fixture"
    layer_file = tmp_path / "layer.fixture"
    manifest_file.write_bytes(manifest)
    layer_file.write_bytes(layer)
    source = OciBundleSource(
        oras_path=_fake_oras(tmp_path),
        work_directory=tmp_path,
        runner=BoundedCommandRunner(),
        environment={
            "ORAS_LOG": str(log),
            "ORAS_MANIFEST_FILE": str(manifest_file),
            "ORAS_LAYER_FILE": str(layer_file),
            "ORAS_MODE": mode,
        },
        command_policy=CommandPolicy(timeout, 1024, stderr_limit),
        required_free_bytes=0,
    )
    return source, log


def _logged(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_fetch_uses_only_raw_digest_manifest_and_blob_commands(tmp_path: Path) -> None:
    layer = b"canonical deployment tar bytes" * 1000
    manifest = _manifest(layer)
    descriptor = _descriptor(manifest, layer)
    source, log = _source(tmp_path, manifest, layer)

    assert source.fetch(descriptor) == layer
    assert _logged(log) == [
        [
            "manifest",
            "fetch",
            "--output",
            "-",
            "--media-type",
            MANIFEST_MEDIA_TYPE,
            descriptor.reference,
        ],
        [
            "blob",
            "fetch",
            "--output",
            "-",
            "registry.example:5443/dgx-forge/control-deployment@"
            + descriptor.layer_digest,
        ],
    ]
    assert all("pull" not in command for command in _logged(log))


def test_fetch_to_streams_into_preopened_sink_and_returns_only_receipt(
    tmp_path: Path,
) -> None:
    layer = b"x" * (2 * 1024 * 1024)
    manifest = _manifest(layer)
    descriptor = _descriptor(manifest, layer)
    source, _log = _source(tmp_path, manifest, layer, timeout=5)
    sink = tmp_path / "bundle.tar"

    with sink.open("w+b") as output:
        receipt = source.fetch_to(descriptor, output.fileno())

    assert receipt.byte_count == len(layer)
    assert receipt.sha256 == hashlib.sha256(layer).hexdigest()
    assert not hasattr(receipt, "content")
    assert sink.read_bytes() == layer


def test_fetch_to_rejects_nonempty_or_linked_sink_before_network(
    tmp_path: Path,
) -> None:
    layer = b"bundle"
    manifest = _manifest(layer)
    descriptor = _descriptor(manifest, layer)
    source, log = _source(tmp_path, manifest, layer)
    sink = tmp_path / "bundle.tar"
    sink.write_bytes(b"must-not-be-destroyed")

    with sink.open("r+b") as output, pytest.raises(OciBundleError, match="sink"):
        source.fetch_to(descriptor, output.fileno())

    assert sink.read_bytes() == b"must-not-be-destroyed"
    assert not log.exists()

    sink.write_bytes(b"")
    os.link(sink, tmp_path / "bundle-hardlink")
    with sink.open("r+b") as output, pytest.raises(OciBundleError, match="sink"):
        source.fetch_to(descriptor, output.fileno())
    assert not log.exists()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"manifest_digest": "sha256:" + "a" * 64}, "manifest digest"),
        ({"manifest_size": 1}, "manifest size"),
        ({"manifest_media_type": "application/example"}, "manifest media type"),
    ],
)
def test_fetch_rejects_manifest_descriptor_disagreement_before_blob(
    tmp_path: Path, change: dict[str, object], message: str
) -> None:
    layer = b"bundle"
    manifest = _manifest(layer)
    descriptor = replace(_descriptor(manifest, layer), **change)
    if "manifest_digest" in change:
        descriptor = replace(
            descriptor,
            reference=descriptor.reference.rsplit("@", 1)[0]
            + "@"
            + str(change["manifest_digest"]),
        )
    source, log = _source(tmp_path, manifest, layer)

    with pytest.raises(OciBundleError, match=message):
        source.fetch(descriptor)

    assert len(_logged(log)) <= 1


@pytest.mark.parametrize(
    "layer_change",
    [
        {"digest": "sha256:" + "a" * 64},
        {"size": 999},
        {"mediaType": "application/example"},
    ],
)
def test_fetch_rejects_layer_not_exactly_bound_by_manifest_before_blob(
    tmp_path: Path, layer_change: dict[str, object]
) -> None:
    layer = b"bundle"
    manifest = _manifest(layer, **layer_change)
    descriptor = replace(
        _descriptor(manifest, layer),
        # The TUF-authorized layer descriptor remains the expected value.
        layer_digest=_digest(layer),
        layer_size=len(layer),
        layer_media_type=LAYER_MEDIA_TYPE,
    )
    source, log = _source(tmp_path, manifest, layer)

    with pytest.raises(OciBundleError, match="layer descriptor"):
        source.fetch(descriptor)

    assert len(_logged(log)) == 1


@pytest.mark.parametrize("actual_layer", [b"bun", b"bundle-with-tampering"])
def test_fetch_rejects_blob_digest_or_size_and_clears_sink(
    tmp_path: Path, actual_layer: bytes
) -> None:
    expected_layer = b"bundle"
    manifest = _manifest(expected_layer)
    descriptor = _descriptor(manifest, expected_layer)
    source, _log = _source(tmp_path, manifest, actual_layer)
    sink = tmp_path / "bundle.tar"

    with sink.open("w+b") as output:
        with pytest.raises(OciBundleError, match="layer (digest|size)"):
            source.fetch_to(descriptor, output.fileno())
        assert output.seek(0, os.SEEK_END) == 0


def test_fetch_rejects_noncanonical_or_ambiguous_manifest_json(tmp_path: Path) -> None:
    layer = b"bundle"
    good = _manifest(layer)
    document = json.loads(good)
    ambiguous = (
        '{"schemaVersion":2,"schemaVersion":2,"mediaType":'
        + json.dumps(MANIFEST_MEDIA_TYPE)
        + ',"layers":'
        + json.dumps(document["layers"])
        + "}"
    ).encode()
    descriptor = _descriptor(ambiguous, layer)
    source, _log = _source(tmp_path, ambiguous, layer)

    with pytest.raises(OciBundleError, match="manifest JSON"):
        source.fetch(descriptor)

    nonfinite = good.replace(
        b'"artifactType":"application/vnd.dgx-forge.control-deployment.v1"',
        b'"artifactType":NaN',
    )
    descriptor = _descriptor(nonfinite, layer)
    source, _log = _source(tmp_path, nonfinite, layer)

    with pytest.raises(OciBundleError, match="manifest JSON"):
        source.fetch(descriptor)


@pytest.mark.parametrize(
    ("mode", "message"), [("fail", "command"), ("sleep", "timeout")]
)
def test_fetch_bounds_and_redacts_oras_failures(
    tmp_path: Path, mode: str, message: str
) -> None:
    layer = b"bundle"
    manifest = _manifest(layer)
    descriptor = _descriptor(manifest, layer)
    source, _log = _source(
        tmp_path,
        manifest,
        layer,
        mode=mode,
        timeout=0.05 if mode == "sleep" else 2,
        stderr_limit=32,
    )

    with pytest.raises(OciBundleError, match=message) as failure:
        source.fetch(descriptor)

    assert "private-token" not in str(failure.value)


def test_fetch_rejects_unbound_reference_without_starting_oras(tmp_path: Path) -> None:
    layer = b"bundle"
    manifest = _manifest(layer)
    descriptor = replace(
        _descriptor(manifest, layer),
        reference="registry.example/dgx-forge/control-deployment:latest",
    )
    source, log = _source(tmp_path, manifest, layer)

    with pytest.raises(OciBundleError, match="descriptor"):
        source.fetch(descriptor)

    assert not log.exists()
