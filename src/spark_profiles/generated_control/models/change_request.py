from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ChangeRequest")



@_attrs_define
class ChangeRequest:
    """
        Attributes:
            proposal_digest (str):
     """

    proposal_digest: str





    def to_dict(self) -> dict[str, Any]:
        proposal_digest = self.proposal_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "proposal_digest": proposal_digest,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        proposal_digest = d.pop("proposal_digest")

        change_request = cls(
            proposal_digest=proposal_digest,
        )

        return change_request
