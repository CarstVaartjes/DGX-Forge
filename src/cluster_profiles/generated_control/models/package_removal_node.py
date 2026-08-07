from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="PackageRemovalNode")



@_attrs_define
class PackageRemovalNode:
    """
        Attributes:
            active (bool):
            leased (bool):
            node_id (str):
            reclaimable_bytes (int):
            retained (bool):
            state (str):
            blocked_reason (Union[None, Unset, str]):
            dependencies (Union[Unset, list[str]]):
     """

    active: bool
    leased: bool
    node_id: str
    reclaimable_bytes: int
    retained: bool
    state: str
    blocked_reason: Union[None, Unset, str] = UNSET
    dependencies: Union[Unset, list[str]] = UNSET





    def to_dict(self) -> dict[str, Any]:
        active = self.active

        leased = self.leased

        node_id = self.node_id

        reclaimable_bytes = self.reclaimable_bytes

        retained = self.retained

        state = self.state

        blocked_reason: Union[None, Unset, str]
        if isinstance(self.blocked_reason, Unset):
            blocked_reason = UNSET
        else:
            blocked_reason = self.blocked_reason

        dependencies: Union[Unset, list[str]] = UNSET
        if not isinstance(self.dependencies, Unset):
            dependencies = self.dependencies




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "active": active,
            "leased": leased,
            "node_id": node_id,
            "reclaimable_bytes": reclaimable_bytes,
            "retained": retained,
            "state": state,
        })
        if blocked_reason is not UNSET:
            field_dict["blocked_reason"] = blocked_reason
        if dependencies is not UNSET:
            field_dict["dependencies"] = dependencies

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        active = d.pop("active")

        leased = d.pop("leased")

        node_id = d.pop("node_id")

        reclaimable_bytes = d.pop("reclaimable_bytes")

        retained = d.pop("retained")

        state = d.pop("state")

        def _parse_blocked_reason(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        blocked_reason = _parse_blocked_reason(d.pop("blocked_reason", UNSET))


        dependencies = cast(list[str], d.pop("dependencies", UNSET))


        package_removal_node = cls(
            active=active,
            leased=leased,
            node_id=node_id,
            reclaimable_bytes=reclaimable_bytes,
            retained=retained,
            state=state,
            blocked_reason=blocked_reason,
            dependencies=dependencies,
        )

        return package_removal_node
