from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="PlanReleaseRequest")



@_attrs_define
class PlanReleaseRequest:
    """
        Attributes:
            adapter_id (str):
            oci_manifest_digest (str):
            provenance_digest (str):
            schema_version (Literal[1]):
            target_digest (str):
            target_name (str):
     """

    adapter_id: str
    oci_manifest_digest: str
    provenance_digest: str
    schema_version: Literal[1]
    target_digest: str
    target_name: str





    def to_dict(self) -> dict[str, Any]:
        adapter_id = self.adapter_id

        oci_manifest_digest = self.oci_manifest_digest

        provenance_digest = self.provenance_digest

        schema_version = self.schema_version

        target_digest = self.target_digest

        target_name = self.target_name


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "adapter_id": adapter_id,
            "oci_manifest_digest": oci_manifest_digest,
            "provenance_digest": provenance_digest,
            "schema_version": schema_version,
            "target_digest": target_digest,
            "target_name": target_name,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        adapter_id = d.pop("adapter_id")

        oci_manifest_digest = d.pop("oci_manifest_digest")

        provenance_digest = d.pop("provenance_digest")

        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        target_digest = d.pop("target_digest")

        target_name = d.pop("target_name")

        plan_release_request = cls(
            adapter_id=adapter_id,
            oci_manifest_digest=oci_manifest_digest,
            provenance_digest=provenance_digest,
            schema_version=schema_version,
            target_digest=target_digest,
            target_name=target_name,
        )

        return plan_release_request
