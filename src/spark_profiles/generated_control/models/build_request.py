from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="BuildRequest")



@_attrs_define
class BuildRequest:
    """
        Attributes:
            build_input_sha256 (str):
            builder_node_id (str):
            recipe_revision_id (str):
            request_key (str):
     """

    build_input_sha256: str
    builder_node_id: str
    recipe_revision_id: str
    request_key: str





    def to_dict(self) -> dict[str, Any]:
        build_input_sha256 = self.build_input_sha256

        builder_node_id = self.builder_node_id

        recipe_revision_id = self.recipe_revision_id

        request_key = self.request_key


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "build_input_sha256": build_input_sha256,
            "builder_node_id": builder_node_id,
            "recipe_revision_id": recipe_revision_id,
            "request_key": request_key,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        build_input_sha256 = d.pop("build_input_sha256")

        builder_node_id = d.pop("builder_node_id")

        recipe_revision_id = d.pop("recipe_revision_id")

        request_key = d.pop("request_key")

        build_request = cls(
            build_input_sha256=build_input_sha256,
            builder_node_id=builder_node_id,
            recipe_revision_id=recipe_revision_id,
            request_key=request_key,
        )

        return build_request
