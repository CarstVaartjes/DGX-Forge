from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ForkRecipeRequest")



@_attrs_define
class ForkRecipeRequest:
    """
        Attributes:
            revision (int):
            slug (str):
     """

    revision: int
    slug: str





    def to_dict(self) -> dict[str, Any]:
        revision = self.revision

        slug = self.slug


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "revision": revision,
            "slug": slug,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        revision = d.pop("revision")

        slug = d.pop("slug")

        fork_recipe_request = cls(
            revision=revision,
            slug=slug,
        )

        return fork_recipe_request
