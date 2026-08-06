from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import Union






T = TypeVar("T", bound="UpdateApproveResumeRequest")



@_attrs_define
class UpdateApproveResumeRequest:
    """
        Attributes:
            reason (Union[Unset, str]):  Default: 'administrator approved update recovery'.
     """

    reason: Union[Unset, str] = 'administrator approved update recovery'





    def to_dict(self) -> dict[str, Any]:
        reason = self.reason


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        reason = d.pop("reason", UNSET)

        update_approve_resume_request = cls(
            reason=reason,
        )

        return update_approve_resume_request
