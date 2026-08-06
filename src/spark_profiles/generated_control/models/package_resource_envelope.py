from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="PackageResourceEnvelope")



@_attrs_define
class PackageResourceEnvelope:
    """ Bounded resource requirements supplied by a promoted workload release.

        Attributes:
            download_bytes (int):
            gpu_memory_bytes (int):
            host_memory_bytes (int):
            installed_bytes (int):
            kv_cache_base_bytes (int):
            kv_cache_per_token_bytes (int):
            required_sparks (int):
            topology (str):
            transient_bytes (int):
     """

    download_bytes: int
    gpu_memory_bytes: int
    host_memory_bytes: int
    installed_bytes: int
    kv_cache_base_bytes: int
    kv_cache_per_token_bytes: int
    required_sparks: int
    topology: str
    transient_bytes: int





    def to_dict(self) -> dict[str, Any]:
        download_bytes = self.download_bytes

        gpu_memory_bytes = self.gpu_memory_bytes

        host_memory_bytes = self.host_memory_bytes

        installed_bytes = self.installed_bytes

        kv_cache_base_bytes = self.kv_cache_base_bytes

        kv_cache_per_token_bytes = self.kv_cache_per_token_bytes

        required_sparks = self.required_sparks

        topology = self.topology

        transient_bytes = self.transient_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "download_bytes": download_bytes,
            "gpu_memory_bytes": gpu_memory_bytes,
            "host_memory_bytes": host_memory_bytes,
            "installed_bytes": installed_bytes,
            "kv_cache_base_bytes": kv_cache_base_bytes,
            "kv_cache_per_token_bytes": kv_cache_per_token_bytes,
            "required_sparks": required_sparks,
            "topology": topology,
            "transient_bytes": transient_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        download_bytes = d.pop("download_bytes")

        gpu_memory_bytes = d.pop("gpu_memory_bytes")

        host_memory_bytes = d.pop("host_memory_bytes")

        installed_bytes = d.pop("installed_bytes")

        kv_cache_base_bytes = d.pop("kv_cache_base_bytes")

        kv_cache_per_token_bytes = d.pop("kv_cache_per_token_bytes")

        required_sparks = d.pop("required_sparks")

        topology = d.pop("topology")

        transient_bytes = d.pop("transient_bytes")

        package_resource_envelope = cls(
            download_bytes=download_bytes,
            gpu_memory_bytes=gpu_memory_bytes,
            host_memory_bytes=host_memory_bytes,
            installed_bytes=installed_bytes,
            kv_cache_base_bytes=kv_cache_base_bytes,
            kv_cache_per_token_bytes=kv_cache_per_token_bytes,
            required_sparks=required_sparks,
            topology=topology,
            transient_bytes=transient_bytes,
        )

        return package_resource_envelope
