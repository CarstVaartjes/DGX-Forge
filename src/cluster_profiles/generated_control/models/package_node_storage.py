from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="PackageNodeStorage")



@_attrs_define
class PackageNodeStorage:
    """
        Attributes:
            free_bytes (int):
            reclaimable_bytes (int):
            reserved_bytes (int):
            total_bytes (int):
            used_bytes (int):
     """

    free_bytes: int
    reclaimable_bytes: int
    reserved_bytes: int
    total_bytes: int
    used_bytes: int





    def to_dict(self) -> dict[str, Any]:
        free_bytes = self.free_bytes

        reclaimable_bytes = self.reclaimable_bytes

        reserved_bytes = self.reserved_bytes

        total_bytes = self.total_bytes

        used_bytes = self.used_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "free_bytes": free_bytes,
            "reclaimable_bytes": reclaimable_bytes,
            "reserved_bytes": reserved_bytes,
            "total_bytes": total_bytes,
            "used_bytes": used_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        free_bytes = d.pop("free_bytes")

        reclaimable_bytes = d.pop("reclaimable_bytes")

        reserved_bytes = d.pop("reserved_bytes")

        total_bytes = d.pop("total_bytes")

        used_bytes = d.pop("used_bytes")

        package_node_storage = cls(
            free_bytes=free_bytes,
            reclaimable_bytes=reclaimable_bytes,
            reserved_bytes=reserved_bytes,
            total_bytes=total_bytes,
            used_bytes=used_bytes,
        )

        return package_node_storage
