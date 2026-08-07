from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.package_node_inventory import PackageNodeInventory





T = TypeVar("T", bound="PackageInventoryResponse")



@_attrs_define
class PackageInventoryResponse:
    """
        Attributes:
            nodes (list['PackageNodeInventory']):
            total (int):
            next_cursor (Union[None, Unset, str]):
     """

    nodes: list['PackageNodeInventory']
    total: int
    next_cursor: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.package_node_inventory import PackageNodeInventory
        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)



        total = self.total

        next_cursor: Union[None, Unset, str]
        if isinstance(self.next_cursor, Unset):
            next_cursor = UNSET
        else:
            next_cursor = self.next_cursor


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "nodes": nodes,
            "total": total,
        })
        if next_cursor is not UNSET:
            field_dict["next_cursor"] = next_cursor

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.package_node_inventory import PackageNodeInventory
        d = dict(src_dict)
        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = PackageNodeInventory.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        total = d.pop("total")

        def _parse_next_cursor(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        next_cursor = _parse_next_cursor(d.pop("next_cursor", UNSET))


        package_inventory_response = cls(
            nodes=nodes,
            total=total,
            next_cursor=next_cursor,
        )

        return package_inventory_response
