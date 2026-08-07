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
  from ..models.package_node_storage import PackageNodeStorage
  from ..models.package_node_resources import PackageNodeResources
  from ..models.package_inventory_item import PackageInventoryItem





T = TypeVar("T", bound="PackageNodeInventory")



@_attrs_define
class PackageNodeInventory:
    """
        Attributes:
            node_id (str):
            online (bool):
            resources (PackageNodeResources):
            storage (PackageNodeStorage):
            current_generation (Union[None, Unset, str]):
            observed_at (Union[None, Unset, str]):
            packages (Union[Unset, list['PackageInventoryItem']]):
     """

    node_id: str
    online: bool
    resources: 'PackageNodeResources'
    storage: 'PackageNodeStorage'
    current_generation: Union[None, Unset, str] = UNSET
    observed_at: Union[None, Unset, str] = UNSET
    packages: Union[Unset, list['PackageInventoryItem']] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.package_node_storage import PackageNodeStorage
        from ..models.package_node_resources import PackageNodeResources
        from ..models.package_inventory_item import PackageInventoryItem
        node_id = self.node_id

        online = self.online

        resources = self.resources.to_dict()

        storage = self.storage.to_dict()

        current_generation: Union[None, Unset, str]
        if isinstance(self.current_generation, Unset):
            current_generation = UNSET
        else:
            current_generation = self.current_generation

        observed_at: Union[None, Unset, str]
        if isinstance(self.observed_at, Unset):
            observed_at = UNSET
        else:
            observed_at = self.observed_at

        packages: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.packages, Unset):
            packages = []
            for packages_item_data in self.packages:
                packages_item = packages_item_data.to_dict()
                packages.append(packages_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_id": node_id,
            "online": online,
            "resources": resources,
            "storage": storage,
        })
        if current_generation is not UNSET:
            field_dict["current_generation"] = current_generation
        if observed_at is not UNSET:
            field_dict["observed_at"] = observed_at
        if packages is not UNSET:
            field_dict["packages"] = packages

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.package_node_storage import PackageNodeStorage
        from ..models.package_node_resources import PackageNodeResources
        from ..models.package_inventory_item import PackageInventoryItem
        d = dict(src_dict)
        node_id = d.pop("node_id")

        online = d.pop("online")

        resources = PackageNodeResources.from_dict(d.pop("resources"))




        storage = PackageNodeStorage.from_dict(d.pop("storage"))




        def _parse_current_generation(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        current_generation = _parse_current_generation(d.pop("current_generation", UNSET))


        def _parse_observed_at(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        observed_at = _parse_observed_at(d.pop("observed_at", UNSET))


        packages = []
        _packages = d.pop("packages", UNSET)
        for packages_item_data in (_packages or []):
            packages_item = PackageInventoryItem.from_dict(packages_item_data)



            packages.append(packages_item)


        package_node_inventory = cls(
            node_id=node_id,
            online=online,
            resources=resources,
            storage=storage,
            current_generation=current_generation,
            observed_at=observed_at,
            packages=packages,
        )

        return package_node_inventory
