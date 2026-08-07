from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="InstallPreviewRequest")



@_attrs_define
class InstallPreviewRequest:
    """
        Attributes:
            node_ids (list[str]):
            recipe_revision_id (str):
     """

    node_ids: list[str]
    recipe_revision_id: str





    def to_dict(self) -> dict[str, Any]:
        node_ids = self.node_ids



        recipe_revision_id = self.recipe_revision_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_ids": node_ids,
            "recipe_revision_id": recipe_revision_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        node_ids = cast(list[str], d.pop("node_ids"))


        recipe_revision_id = d.pop("recipe_revision_id")

        install_preview_request = cls(
            node_ids=node_ids,
            recipe_revision_id=recipe_revision_id,
        )

        return install_preview_request
