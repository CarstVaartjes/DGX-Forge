from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.placement_request_role import check_placement_request_role
from ..models.placement_request_role import PlacementRequestRole
from typing import cast






T = TypeVar("T", bound="PlacementRequest")



@_attrs_define
class PlacementRequest:
    """
        Attributes:
            node_id (str):
            rank (int):
            role (PlacementRequestRole):
     """

    node_id: str
    rank: int
    role: PlacementRequestRole





    def to_dict(self) -> dict[str, Any]:
        node_id = self.node_id

        rank = self.rank

        role: str = self.role


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_id": node_id,
            "rank": rank,
            "role": role,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        node_id = d.pop("node_id")

        rank = d.pop("rank")

        role = check_placement_request_role(d.pop("role"))




        placement_request = cls(
            node_id=node_id,
            rank=rank,
            role=role,
        )

        return placement_request
