from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="PackageFamilyResponse")



@_attrs_define
class PackageFamilyResponse:
    """
        Attributes:
            channels (list[str]):
            id (str):
            promotion_mode (str):
     """

    channels: list[str]
    id: str
    promotion_mode: str





    def to_dict(self) -> dict[str, Any]:
        channels = self.channels



        id = self.id

        promotion_mode = self.promotion_mode


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "channels": channels,
            "id": id,
            "promotion_mode": promotion_mode,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        channels = cast(list[str], d.pop("channels"))


        id = d.pop("id")

        promotion_mode = d.pop("promotion_mode")

        package_family_response = cls(
            channels=channels,
            id=id,
            promotion_mode=promotion_mode,
        )

        return package_family_response
