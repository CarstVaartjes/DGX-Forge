from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="UpdateApplyRequest")



@_attrs_define
class UpdateApplyRequest:
    """
        Attributes:
            plan_digest (str):
     """

    plan_digest: str





    def to_dict(self) -> dict[str, Any]:
        plan_digest = self.plan_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "plan_digest": plan_digest,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        plan_digest = d.pop("plan_digest")

        update_apply_request = cls(
            plan_digest=plan_digest,
        )

        return update_apply_request
