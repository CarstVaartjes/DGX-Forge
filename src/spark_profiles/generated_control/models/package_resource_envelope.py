from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.package_rank import PackageRank
  from ..models.package_fabric import PackageFabric





T = TypeVar("T", bound="PackageResourceEnvelope")



@_attrs_define
class PackageResourceEnvelope:
    """ Bounded per-Spark resource requirements from a promoted release.

        Attributes:
            activation_memory_bytes (int):
            auxiliary_memory_bytes (int):
            cpu_millicores (int):
            download_bytes (int):
            fabric (PackageFabric):
            gpu_count (int):
            gpu_memory_bytes (int):
            host_memory_bytes (int):
            installed_bytes (int):
            kv_cache_base_bytes (int):
            kv_cache_per_token_bytes (int):
            output_bytes (int):
            ranks (list['PackageRank']):
            required_sparks (int):
            resident_memory_bytes (int):
            topology (str):
            transient_bytes (int):
            workspace_memory_bytes (int):
            world_size (int):
     """

    activation_memory_bytes: int
    auxiliary_memory_bytes: int
    cpu_millicores: int
    download_bytes: int
    fabric: 'PackageFabric'
    gpu_count: int
    gpu_memory_bytes: int
    host_memory_bytes: int
    installed_bytes: int
    kv_cache_base_bytes: int
    kv_cache_per_token_bytes: int
    output_bytes: int
    ranks: list['PackageRank']
    required_sparks: int
    resident_memory_bytes: int
    topology: str
    transient_bytes: int
    workspace_memory_bytes: int
    world_size: int





    def to_dict(self) -> dict[str, Any]:
        from ..models.package_rank import PackageRank
        from ..models.package_fabric import PackageFabric
        activation_memory_bytes = self.activation_memory_bytes

        auxiliary_memory_bytes = self.auxiliary_memory_bytes

        cpu_millicores = self.cpu_millicores

        download_bytes = self.download_bytes

        fabric = self.fabric.to_dict()

        gpu_count = self.gpu_count

        gpu_memory_bytes = self.gpu_memory_bytes

        host_memory_bytes = self.host_memory_bytes

        installed_bytes = self.installed_bytes

        kv_cache_base_bytes = self.kv_cache_base_bytes

        kv_cache_per_token_bytes = self.kv_cache_per_token_bytes

        output_bytes = self.output_bytes

        ranks = []
        for ranks_item_data in self.ranks:
            ranks_item = ranks_item_data.to_dict()
            ranks.append(ranks_item)



        required_sparks = self.required_sparks

        resident_memory_bytes = self.resident_memory_bytes

        topology = self.topology

        transient_bytes = self.transient_bytes

        workspace_memory_bytes = self.workspace_memory_bytes

        world_size = self.world_size


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "activation_memory_bytes": activation_memory_bytes,
            "auxiliary_memory_bytes": auxiliary_memory_bytes,
            "cpu_millicores": cpu_millicores,
            "download_bytes": download_bytes,
            "fabric": fabric,
            "gpu_count": gpu_count,
            "gpu_memory_bytes": gpu_memory_bytes,
            "host_memory_bytes": host_memory_bytes,
            "installed_bytes": installed_bytes,
            "kv_cache_base_bytes": kv_cache_base_bytes,
            "kv_cache_per_token_bytes": kv_cache_per_token_bytes,
            "output_bytes": output_bytes,
            "ranks": ranks,
            "required_sparks": required_sparks,
            "resident_memory_bytes": resident_memory_bytes,
            "topology": topology,
            "transient_bytes": transient_bytes,
            "workspace_memory_bytes": workspace_memory_bytes,
            "world_size": world_size,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.package_rank import PackageRank
        from ..models.package_fabric import PackageFabric
        d = dict(src_dict)
        activation_memory_bytes = d.pop("activation_memory_bytes")

        auxiliary_memory_bytes = d.pop("auxiliary_memory_bytes")

        cpu_millicores = d.pop("cpu_millicores")

        download_bytes = d.pop("download_bytes")

        fabric = PackageFabric.from_dict(d.pop("fabric"))




        gpu_count = d.pop("gpu_count")

        gpu_memory_bytes = d.pop("gpu_memory_bytes")

        host_memory_bytes = d.pop("host_memory_bytes")

        installed_bytes = d.pop("installed_bytes")

        kv_cache_base_bytes = d.pop("kv_cache_base_bytes")

        kv_cache_per_token_bytes = d.pop("kv_cache_per_token_bytes")

        output_bytes = d.pop("output_bytes")

        ranks = []
        _ranks = d.pop("ranks")
        for ranks_item_data in (_ranks):
            ranks_item = PackageRank.from_dict(ranks_item_data)



            ranks.append(ranks_item)


        required_sparks = d.pop("required_sparks")

        resident_memory_bytes = d.pop("resident_memory_bytes")

        topology = d.pop("topology")

        transient_bytes = d.pop("transient_bytes")

        workspace_memory_bytes = d.pop("workspace_memory_bytes")

        world_size = d.pop("world_size")

        package_resource_envelope = cls(
            activation_memory_bytes=activation_memory_bytes,
            auxiliary_memory_bytes=auxiliary_memory_bytes,
            cpu_millicores=cpu_millicores,
            download_bytes=download_bytes,
            fabric=fabric,
            gpu_count=gpu_count,
            gpu_memory_bytes=gpu_memory_bytes,
            host_memory_bytes=host_memory_bytes,
            installed_bytes=installed_bytes,
            kv_cache_base_bytes=kv_cache_base_bytes,
            kv_cache_per_token_bytes=kv_cache_per_token_bytes,
            output_bytes=output_bytes,
            ranks=ranks,
            required_sparks=required_sparks,
            resident_memory_bytes=resident_memory_bytes,
            topology=topology,
            transient_bytes=transient_bytes,
            workspace_memory_bytes=workspace_memory_bytes,
            world_size=world_size,
        )

        return package_resource_envelope
