from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ImageDistributionRequest")



@_attrs_define
class ImageDistributionRequest:
    """
        Attributes:
            mapping_generation (int):
            mapping_id (str):
            recipe_build_id (str):
            request_key (str):
     """

    mapping_generation: int
    mapping_id: str
    recipe_build_id: str
    request_key: str





    def to_dict(self) -> dict[str, Any]:
        mapping_generation = self.mapping_generation

        mapping_id = self.mapping_id

        recipe_build_id = self.recipe_build_id

        request_key = self.request_key


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "mapping_generation": mapping_generation,
            "mapping_id": mapping_id,
            "recipe_build_id": recipe_build_id,
            "request_key": request_key,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        mapping_generation = d.pop("mapping_generation")

        mapping_id = d.pop("mapping_id")

        recipe_build_id = d.pop("recipe_build_id")

        request_key = d.pop("request_key")

        image_distribution_request = cls(
            mapping_generation=mapping_generation,
            mapping_id=mapping_id,
            recipe_build_id=recipe_build_id,
            request_key=request_key,
        )

        return image_distribution_request
