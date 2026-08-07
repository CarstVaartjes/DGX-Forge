from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="BuildPlanResponse")



@_attrs_define
class BuildPlanResponse:
    """
        Attributes:
            build_id (str):
            build_input_sha256 (str):
            builder_node_id (str):
            recipe_content_sha256 (str):
            recipe_revision_id (str):
            source_bundle_sha256 (str):
     """

    build_id: str
    build_input_sha256: str
    builder_node_id: str
    recipe_content_sha256: str
    recipe_revision_id: str
    source_bundle_sha256: str





    def to_dict(self) -> dict[str, Any]:
        build_id = self.build_id

        build_input_sha256 = self.build_input_sha256

        builder_node_id = self.builder_node_id

        recipe_content_sha256 = self.recipe_content_sha256

        recipe_revision_id = self.recipe_revision_id

        source_bundle_sha256 = self.source_bundle_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "build_id": build_id,
            "build_input_sha256": build_input_sha256,
            "builder_node_id": builder_node_id,
            "recipe_content_sha256": recipe_content_sha256,
            "recipe_revision_id": recipe_revision_id,
            "source_bundle_sha256": source_bundle_sha256,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        build_id = d.pop("build_id")

        build_input_sha256 = d.pop("build_input_sha256")

        builder_node_id = d.pop("builder_node_id")

        recipe_content_sha256 = d.pop("recipe_content_sha256")

        recipe_revision_id = d.pop("recipe_revision_id")

        source_bundle_sha256 = d.pop("source_bundle_sha256")

        build_plan_response = cls(
            build_id=build_id,
            build_input_sha256=build_input_sha256,
            builder_node_id=builder_node_id,
            recipe_content_sha256=recipe_content_sha256,
            recipe_revision_id=recipe_revision_id,
            source_bundle_sha256=source_bundle_sha256,
        )

        return build_plan_response
