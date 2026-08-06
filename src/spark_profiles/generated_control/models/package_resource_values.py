from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="PackageResourceValues")



@_attrs_define
class PackageResourceValues:
    """
        Attributes:
            activation_memory_bytes (int):
            auxiliary_memory_bytes (int):
            cpu_millicores (int):
            download_bytes (int):
            gpu_count (int):
            gpu_memory_bytes (int):
            host_memory_bytes (int):
            installed_bytes (int):
            kv_cache_base_bytes (int):
            kv_cache_per_token_bytes (int):
            output_bytes (int):
            resident_memory_bytes (int):
            transient_bytes (int):
            workspace_memory_bytes (int):
     """

    activation_memory_bytes: int
    auxiliary_memory_bytes: int
    cpu_millicores: int
    download_bytes: int
    gpu_count: int
    gpu_memory_bytes: int
    host_memory_bytes: int
    installed_bytes: int
    kv_cache_base_bytes: int
    kv_cache_per_token_bytes: int
    output_bytes: int
    resident_memory_bytes: int
    transient_bytes: int
    workspace_memory_bytes: int





    def to_dict(self) -> dict[str, Any]:
        activation_memory_bytes = self.activation_memory_bytes

        auxiliary_memory_bytes = self.auxiliary_memory_bytes

        cpu_millicores = self.cpu_millicores

        download_bytes = self.download_bytes

        gpu_count = self.gpu_count

        gpu_memory_bytes = self.gpu_memory_bytes

        host_memory_bytes = self.host_memory_bytes

        installed_bytes = self.installed_bytes

        kv_cache_base_bytes = self.kv_cache_base_bytes

        kv_cache_per_token_bytes = self.kv_cache_per_token_bytes

        output_bytes = self.output_bytes

        resident_memory_bytes = self.resident_memory_bytes

        transient_bytes = self.transient_bytes

        workspace_memory_bytes = self.workspace_memory_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "activation_memory_bytes": activation_memory_bytes,
            "auxiliary_memory_bytes": auxiliary_memory_bytes,
            "cpu_millicores": cpu_millicores,
            "download_bytes": download_bytes,
            "gpu_count": gpu_count,
            "gpu_memory_bytes": gpu_memory_bytes,
            "host_memory_bytes": host_memory_bytes,
            "installed_bytes": installed_bytes,
            "kv_cache_base_bytes": kv_cache_base_bytes,
            "kv_cache_per_token_bytes": kv_cache_per_token_bytes,
            "output_bytes": output_bytes,
            "resident_memory_bytes": resident_memory_bytes,
            "transient_bytes": transient_bytes,
            "workspace_memory_bytes": workspace_memory_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        activation_memory_bytes = d.pop("activation_memory_bytes")

        auxiliary_memory_bytes = d.pop("auxiliary_memory_bytes")

        cpu_millicores = d.pop("cpu_millicores")

        download_bytes = d.pop("download_bytes")

        gpu_count = d.pop("gpu_count")

        gpu_memory_bytes = d.pop("gpu_memory_bytes")

        host_memory_bytes = d.pop("host_memory_bytes")

        installed_bytes = d.pop("installed_bytes")

        kv_cache_base_bytes = d.pop("kv_cache_base_bytes")

        kv_cache_per_token_bytes = d.pop("kv_cache_per_token_bytes")

        output_bytes = d.pop("output_bytes")

        resident_memory_bytes = d.pop("resident_memory_bytes")

        transient_bytes = d.pop("transient_bytes")

        workspace_memory_bytes = d.pop("workspace_memory_bytes")

        package_resource_values = cls(
            activation_memory_bytes=activation_memory_bytes,
            auxiliary_memory_bytes=auxiliary_memory_bytes,
            cpu_millicores=cpu_millicores,
            download_bytes=download_bytes,
            gpu_count=gpu_count,
            gpu_memory_bytes=gpu_memory_bytes,
            host_memory_bytes=host_memory_bytes,
            installed_bytes=installed_bytes,
            kv_cache_base_bytes=kv_cache_base_bytes,
            kv_cache_per_token_bytes=kv_cache_per_token_bytes,
            output_bytes=output_bytes,
            resident_memory_bytes=resident_memory_bytes,
            transient_bytes=transient_bytes,
            workspace_memory_bytes=workspace_memory_bytes,
        )

        return package_resource_values
