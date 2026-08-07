from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="PackageNodeResources")



@_attrs_define
class PackageNodeResources:
    """
        Attributes:
            gpu_count (int):
            gpu_memory_free_bytes (int):
            gpu_memory_total_bytes (int):
            host_memory_free_bytes (int):
            host_memory_total_bytes (int):
     """

    gpu_count: int
    gpu_memory_free_bytes: int
    gpu_memory_total_bytes: int
    host_memory_free_bytes: int
    host_memory_total_bytes: int





    def to_dict(self) -> dict[str, Any]:
        gpu_count = self.gpu_count

        gpu_memory_free_bytes = self.gpu_memory_free_bytes

        gpu_memory_total_bytes = self.gpu_memory_total_bytes

        host_memory_free_bytes = self.host_memory_free_bytes

        host_memory_total_bytes = self.host_memory_total_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "gpu_count": gpu_count,
            "gpu_memory_free_bytes": gpu_memory_free_bytes,
            "gpu_memory_total_bytes": gpu_memory_total_bytes,
            "host_memory_free_bytes": host_memory_free_bytes,
            "host_memory_total_bytes": host_memory_total_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        gpu_count = d.pop("gpu_count")

        gpu_memory_free_bytes = d.pop("gpu_memory_free_bytes")

        gpu_memory_total_bytes = d.pop("gpu_memory_total_bytes")

        host_memory_free_bytes = d.pop("host_memory_free_bytes")

        host_memory_total_bytes = d.pop("host_memory_total_bytes")

        package_node_resources = cls(
            gpu_count=gpu_count,
            gpu_memory_free_bytes=gpu_memory_free_bytes,
            gpu_memory_total_bytes=gpu_memory_total_bytes,
            host_memory_free_bytes=host_memory_free_bytes,
            host_memory_total_bytes=host_memory_total_bytes,
        )

        return package_node_resources
