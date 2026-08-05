from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="PlanOperation")



@_attrs_define
class PlanOperation:
    """
        Attributes:
            dependencies (list[str]):
            kind (str):
            node_id (str):
            operation_id (str):
            payload_digest (str):
            workload_id (str):
            compensation_kind (Union[None, Unset, str]):
     """

    dependencies: list[str]
    kind: str
    node_id: str
    operation_id: str
    payload_digest: str
    workload_id: str
    compensation_kind: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        dependencies = self.dependencies



        kind = self.kind

        node_id = self.node_id

        operation_id = self.operation_id

        payload_digest = self.payload_digest

        workload_id = self.workload_id

        compensation_kind: Union[None, Unset, str]
        if isinstance(self.compensation_kind, Unset):
            compensation_kind = UNSET
        else:
            compensation_kind = self.compensation_kind


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "dependencies": dependencies,
            "kind": kind,
            "node_id": node_id,
            "operation_id": operation_id,
            "payload_digest": payload_digest,
            "workload_id": workload_id,
        })
        if compensation_kind is not UNSET:
            field_dict["compensation_kind"] = compensation_kind

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        dependencies = cast(list[str], d.pop("dependencies"))


        kind = d.pop("kind")

        node_id = d.pop("node_id")

        operation_id = d.pop("operation_id")

        payload_digest = d.pop("payload_digest")

        workload_id = d.pop("workload_id")

        def _parse_compensation_kind(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        compensation_kind = _parse_compensation_kind(d.pop("compensation_kind", UNSET))


        plan_operation = cls(
            dependencies=dependencies,
            kind=kind,
            node_id=node_id,
            operation_id=operation_id,
            payload_digest=payload_digest,
            workload_id=workload_id,
            compensation_kind=compensation_kind,
        )

        return plan_operation
