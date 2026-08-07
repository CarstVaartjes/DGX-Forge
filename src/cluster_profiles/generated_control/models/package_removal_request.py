from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="PackageRemovalRequest")



@_attrs_define
class PackageRemovalRequest:
    """
        Attributes:
            deployment_id (str):
            node_ids (list[str]):
            release_digest (str):
     """

    deployment_id: str
    node_ids: list[str]
    release_digest: str





    def to_dict(self) -> dict[str, Any]:
        deployment_id = self.deployment_id

        node_ids = self.node_ids



        release_digest = self.release_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "deployment_id": deployment_id,
            "node_ids": node_ids,
            "release_digest": release_digest,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        deployment_id = d.pop("deployment_id")

        node_ids = cast(list[str], d.pop("node_ids"))


        release_digest = d.pop("release_digest")

        package_removal_request = cls(
            deployment_id=deployment_id,
            node_ids=node_ids,
            release_digest=release_digest,
        )

        return package_removal_request
