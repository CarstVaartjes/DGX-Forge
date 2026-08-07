from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="PackageCompatibilityResponse")



@_attrs_define
class PackageCompatibilityResponse:
    """
        Attributes:
            candidate_id (str):
            compatible_node_ids (list[str]):
            digest (str):
            release_digest (str):
     """

    candidate_id: str
    compatible_node_ids: list[str]
    digest: str
    release_digest: str





    def to_dict(self) -> dict[str, Any]:
        candidate_id = self.candidate_id

        compatible_node_ids = self.compatible_node_ids



        digest = self.digest

        release_digest = self.release_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "candidate_id": candidate_id,
            "compatible_node_ids": compatible_node_ids,
            "digest": digest,
            "release_digest": release_digest,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        candidate_id = d.pop("candidate_id")

        compatible_node_ids = cast(list[str], d.pop("compatible_node_ids"))


        digest = d.pop("digest")

        release_digest = d.pop("release_digest")

        package_compatibility_response = cls(
            candidate_id=candidate_id,
            compatible_node_ids=compatible_node_ids,
            digest=digest,
            release_digest=release_digest,
        )

        return package_compatibility_response
