from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="PlanStartRequest")



@_attrs_define
class PlanStartRequest:
    """
        Attributes:
            adapter_id (str):
            preparation_digest (str):
            release_digest (str):
            schema_version (Literal[1]):
            workload_id (str):
     """

    adapter_id: str
    preparation_digest: str
    release_digest: str
    schema_version: Literal[1]
    workload_id: str





    def to_dict(self) -> dict[str, Any]:
        adapter_id = self.adapter_id

        preparation_digest = self.preparation_digest

        release_digest = self.release_digest

        schema_version = self.schema_version

        workload_id = self.workload_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "adapter_id": adapter_id,
            "preparation_digest": preparation_digest,
            "release_digest": release_digest,
            "schema_version": schema_version,
            "workload_id": workload_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        adapter_id = d.pop("adapter_id")

        preparation_digest = d.pop("preparation_digest")

        release_digest = d.pop("release_digest")

        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        workload_id = d.pop("workload_id")

        plan_start_request = cls(
            adapter_id=adapter_id,
            preparation_digest=preparation_digest,
            release_digest=release_digest,
            schema_version=schema_version,
            workload_id=workload_id,
        )

        return plan_start_request
