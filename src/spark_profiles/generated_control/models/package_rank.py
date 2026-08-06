from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="PackageRank")



@_attrs_define
class PackageRank:
    """
        Attributes:
            rank (int):
            role (str):
     """

    rank: int
    role: str





    def to_dict(self) -> dict[str, Any]:
        rank = self.rank

        role = self.role


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "rank": rank,
            "role": role,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        rank = d.pop("rank")

        role = d.pop("role")

        package_rank = cls(
            rank=rank,
            role=role,
        )

        return package_rank
