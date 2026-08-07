from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="SourceCheckRequest")



@_attrs_define
class SourceCheckRequest:
    """
        Attributes:
            recipe_revision_id (str):
     """

    recipe_revision_id: str





    def to_dict(self) -> dict[str, Any]:
        recipe_revision_id = self.recipe_revision_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "recipe_revision_id": recipe_revision_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        recipe_revision_id = d.pop("recipe_revision_id")

        source_check_request = cls(
            recipe_revision_id=recipe_revision_id,
        )

        return source_check_request
