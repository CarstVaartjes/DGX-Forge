from pathlib import Path

from cluster_profiles.catalog import Catalog

PHASE4_IDS = {
    "nemotron-super-single",
    "nemotron-nano-omni-single",
    "qwen-image-single",
    "qwen-image-edit-2511-single",
    "pixal3d-single",
    "trellis2-4b-single",
    "qwen3-vl-8b-single",
    "tokenrig-single",
    "step1x-3d-single",
    "triposg-single",
    "hunyuan3d-omni-single",
    "laguna-s21-single",
}


def test_phase4_definitions_are_cataloged_as_planned() -> None:
    root = Path(__file__).parents[2]
    catalog = Catalog.load(root)
    definitions = catalog.definitions
    assert PHASE4_IDS <= definitions.keys()
    assert all(definitions[identifier].nodes == ("node2",) for identifier in PHASE4_IDS)
