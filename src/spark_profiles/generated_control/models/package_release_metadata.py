from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Union

if TYPE_CHECKING:
  from ..models.package_component_response import PackageComponentResponse
  from ..models.package_provenance_response import PackageProvenanceResponse





T = TypeVar("T", bound="PackageReleaseMetadata")



@_attrs_define
class PackageReleaseMetadata:
    """
        Attributes:
            lock_digest (str):
            release_digest (str):
            components (Union[Unset, list['PackageComponentResponse']]):
            dependencies (Union[Unset, list[str]]):
            provenance (Union[Unset, list['PackageProvenanceResponse']]):
     """

    lock_digest: str
    release_digest: str
    components: Union[Unset, list['PackageComponentResponse']] = UNSET
    dependencies: Union[Unset, list[str]] = UNSET
    provenance: Union[Unset, list['PackageProvenanceResponse']] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.package_component_response import PackageComponentResponse
        from ..models.package_provenance_response import PackageProvenanceResponse
        lock_digest = self.lock_digest

        release_digest = self.release_digest

        components: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.components, Unset):
            components = []
            for components_item_data in self.components:
                components_item = components_item_data.to_dict()
                components.append(components_item)



        dependencies: Union[Unset, list[str]] = UNSET
        if not isinstance(self.dependencies, Unset):
            dependencies = self.dependencies



        provenance: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.provenance, Unset):
            provenance = []
            for provenance_item_data in self.provenance:
                provenance_item = provenance_item_data.to_dict()
                provenance.append(provenance_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "lock_digest": lock_digest,
            "release_digest": release_digest,
        })
        if components is not UNSET:
            field_dict["components"] = components
        if dependencies is not UNSET:
            field_dict["dependencies"] = dependencies
        if provenance is not UNSET:
            field_dict["provenance"] = provenance

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.package_component_response import PackageComponentResponse
        from ..models.package_provenance_response import PackageProvenanceResponse
        d = dict(src_dict)
        lock_digest = d.pop("lock_digest")

        release_digest = d.pop("release_digest")

        components = []
        _components = d.pop("components", UNSET)
        for components_item_data in (_components or []):
            components_item = PackageComponentResponse.from_dict(components_item_data)



            components.append(components_item)


        dependencies = cast(list[str], d.pop("dependencies", UNSET))


        provenance = []
        _provenance = d.pop("provenance", UNSET)
        for provenance_item_data in (_provenance or []):
            provenance_item = PackageProvenanceResponse.from_dict(provenance_item_data)



            provenance.append(provenance_item)


        package_release_metadata = cls(
            lock_digest=lock_digest,
            release_digest=release_digest,
            components=components,
            dependencies=dependencies,
            provenance=provenance,
        )

        return package_release_metadata
