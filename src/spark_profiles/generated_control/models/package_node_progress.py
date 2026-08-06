from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="PackageNodeProgress")



@_attrs_define
class PackageNodeProgress:
    """
        Attributes:
            batch_index (int):
            completed (int):
            node_id (str):
            state (str):
            total (int):
     """

    batch_index: int
    completed: int
    node_id: str
    state: str
    total: int





    def to_dict(self) -> dict[str, Any]:
        batch_index = self.batch_index

        completed = self.completed

        node_id = self.node_id

        state = self.state

        total = self.total


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "batch_index": batch_index,
            "completed": completed,
            "node_id": node_id,
            "state": state,
            "total": total,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        batch_index = d.pop("batch_index")

        completed = d.pop("completed")

        node_id = d.pop("node_id")

        state = d.pop("state")

        total = d.pop("total")

        package_node_progress = cls(
            batch_index=batch_index,
            completed=completed,
            node_id=node_id,
            state=state,
            total=total,
        )

        return package_node_progress
