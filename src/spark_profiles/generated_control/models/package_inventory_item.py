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
  from ..models.package_resource_envelope import PackageResourceEnvelope





T = TypeVar("T", bound="PackageInventoryItem")



@_attrs_define
class PackageInventoryItem:
    """ One release/content group as observed on one Spark.

        Attributes:
            active (bool):
            bytes_complete (int):
            bytes_remaining (int):
            bytes_total (int):
            content_group (str):
            deployment_id (str):
            installed_bytes (int):
            leased (bool):
            reclaimable_bytes (int):
            release_digest (str):
            reserved_bytes (int):
            resources (PackageResourceEnvelope): Bounded resource requirements supplied by a promoted workload release.
            retained (bool):
            state (str):
            family_id (Union[None, Unset, str]):
            last_operation_error (Union[None, Unset, str]):
            last_operation_state (Union[None, Unset, str]):
            operation_id (Union[None, Unset, str]):
     """

    active: bool
    bytes_complete: int
    bytes_remaining: int
    bytes_total: int
    content_group: str
    deployment_id: str
    installed_bytes: int
    leased: bool
    reclaimable_bytes: int
    release_digest: str
    reserved_bytes: int
    resources: 'PackageResourceEnvelope'
    retained: bool
    state: str
    family_id: Union[None, Unset, str] = UNSET
    last_operation_error: Union[None, Unset, str] = UNSET
    last_operation_state: Union[None, Unset, str] = UNSET
    operation_id: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.package_resource_envelope import PackageResourceEnvelope
        active = self.active

        bytes_complete = self.bytes_complete

        bytes_remaining = self.bytes_remaining

        bytes_total = self.bytes_total

        content_group = self.content_group

        deployment_id = self.deployment_id

        installed_bytes = self.installed_bytes

        leased = self.leased

        reclaimable_bytes = self.reclaimable_bytes

        release_digest = self.release_digest

        reserved_bytes = self.reserved_bytes

        resources = self.resources.to_dict()

        retained = self.retained

        state = self.state

        family_id: Union[None, Unset, str]
        if isinstance(self.family_id, Unset):
            family_id = UNSET
        else:
            family_id = self.family_id

        last_operation_error: Union[None, Unset, str]
        if isinstance(self.last_operation_error, Unset):
            last_operation_error = UNSET
        else:
            last_operation_error = self.last_operation_error

        last_operation_state: Union[None, Unset, str]
        if isinstance(self.last_operation_state, Unset):
            last_operation_state = UNSET
        else:
            last_operation_state = self.last_operation_state

        operation_id: Union[None, Unset, str]
        if isinstance(self.operation_id, Unset):
            operation_id = UNSET
        else:
            operation_id = self.operation_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "active": active,
            "bytes_complete": bytes_complete,
            "bytes_remaining": bytes_remaining,
            "bytes_total": bytes_total,
            "content_group": content_group,
            "deployment_id": deployment_id,
            "installed_bytes": installed_bytes,
            "leased": leased,
            "reclaimable_bytes": reclaimable_bytes,
            "release_digest": release_digest,
            "reserved_bytes": reserved_bytes,
            "resources": resources,
            "retained": retained,
            "state": state,
        })
        if family_id is not UNSET:
            field_dict["family_id"] = family_id
        if last_operation_error is not UNSET:
            field_dict["last_operation_error"] = last_operation_error
        if last_operation_state is not UNSET:
            field_dict["last_operation_state"] = last_operation_state
        if operation_id is not UNSET:
            field_dict["operation_id"] = operation_id

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.package_resource_envelope import PackageResourceEnvelope
        d = dict(src_dict)
        active = d.pop("active")

        bytes_complete = d.pop("bytes_complete")

        bytes_remaining = d.pop("bytes_remaining")

        bytes_total = d.pop("bytes_total")

        content_group = d.pop("content_group")

        deployment_id = d.pop("deployment_id")

        installed_bytes = d.pop("installed_bytes")

        leased = d.pop("leased")

        reclaimable_bytes = d.pop("reclaimable_bytes")

        release_digest = d.pop("release_digest")

        reserved_bytes = d.pop("reserved_bytes")

        resources = PackageResourceEnvelope.from_dict(d.pop("resources"))




        retained = d.pop("retained")

        state = d.pop("state")

        def _parse_family_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        family_id = _parse_family_id(d.pop("family_id", UNSET))


        def _parse_last_operation_error(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        last_operation_error = _parse_last_operation_error(d.pop("last_operation_error", UNSET))


        def _parse_last_operation_state(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        last_operation_state = _parse_last_operation_state(d.pop("last_operation_state", UNSET))


        def _parse_operation_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        operation_id = _parse_operation_id(d.pop("operation_id", UNSET))


        package_inventory_item = cls(
            active=active,
            bytes_complete=bytes_complete,
            bytes_remaining=bytes_remaining,
            bytes_total=bytes_total,
            content_group=content_group,
            deployment_id=deployment_id,
            installed_bytes=installed_bytes,
            leased=leased,
            reclaimable_bytes=reclaimable_bytes,
            release_digest=release_digest,
            reserved_bytes=reserved_bytes,
            resources=resources,
            retained=retained,
            state=state,
            family_id=family_id,
            last_operation_error=last_operation_error,
            last_operation_state=last_operation_state,
            operation_id=operation_id,
        )

        return package_inventory_item
