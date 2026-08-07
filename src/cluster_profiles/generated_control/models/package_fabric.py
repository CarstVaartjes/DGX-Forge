from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="PackageFabric")



@_attrs_define
class PackageFabric:
    """
        Attributes:
            kind (str):
            min_bandwidth_mbps (int):
     """

    kind: str
    min_bandwidth_mbps: int





    def to_dict(self) -> dict[str, Any]:
        kind = self.kind

        min_bandwidth_mbps = self.min_bandwidth_mbps


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "kind": kind,
            "min_bandwidth_mbps": min_bandwidth_mbps,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kind = d.pop("kind")

        min_bandwidth_mbps = d.pop("min_bandwidth_mbps")

        package_fabric = cls(
            kind=kind,
            min_bandwidth_mbps=min_bandwidth_mbps,
        )

        return package_fabric
