from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="PackagePromotionResponse")



@_attrs_define
class PackagePromotionResponse:
    """
        Attributes:
            candidate_id (str):
            digest (str):
            release_digest (str):
            state (str):
     """

    candidate_id: str
    digest: str
    release_digest: str
    state: str





    def to_dict(self) -> dict[str, Any]:
        candidate_id = self.candidate_id

        digest = self.digest

        release_digest = self.release_digest

        state = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "candidate_id": candidate_id,
            "digest": digest,
            "release_digest": release_digest,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        candidate_id = d.pop("candidate_id")

        digest = d.pop("digest")

        release_digest = d.pop("release_digest")

        state = d.pop("state")

        package_promotion_response = cls(
            candidate_id=candidate_id,
            digest=digest,
            release_digest=release_digest,
            state=state,
        )

        return package_promotion_response
