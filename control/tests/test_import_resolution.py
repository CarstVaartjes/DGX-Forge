import hashlib
import json
from pathlib import Path

from dgx_control.import_resolution import resolve_import
from dgx_control.model_resolution import ModelFile, SnapshotEnvelope
from dgx_control.registry_resolution import ManifestEnvelope
from dgx_control.sparkrun_importer import import_sparkrun
from dgx_control.sparkrun_source import parse_sparkrun_yaml


class Registry:
    def resolve(self, host): return ("93.184.216.34",)
    def manifest(self, host, repository, reference, *, maximum_bytes):
        document = {"mediaType": "application/vnd.oci.image.manifest.v1+json", "layers": [{"digest": "sha256:" + "b" * 64, "size": 50}]}
        body = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        return ManifestEnvelope(body, "sha256:" + hashlib.sha256(body).hexdigest(), document["mediaType"], "linux/arm64", ())


class Models:
    def snapshot(self, repository, revision, *, maximum_files):
        return SnapshotEnvelope(repository, revision, (ModelFile("model.safetensors", 100),))


def test_complete_overlays_resolve_import_to_valid_recipe() -> None:
    raw = (Path(__file__).parent / "fixtures/sparkrun/minimal-vllm.yaml").read_bytes()
    imported = import_sparkrun(parse_sparkrun_yaml(raw))
    result = resolve_import(imported, {
        "resources": {"download_bytes": 100, "installed_bytes": 150, "staging_bytes": 50, "resident_memory_bytes": 200, "activation_memory_bytes": 25},
        "security_acknowledged": True,
    }, registry=Registry(), models=Models())

    assert result.runnable is True
    assert result.document["runtime"]["image"].startswith("registry.example/") is False
    assert "@sha256:" in result.document["runtime"]["image"]
    assert result.document["artifacts"][0]["expected_bytes"] == 100
    assert not result.blockers
