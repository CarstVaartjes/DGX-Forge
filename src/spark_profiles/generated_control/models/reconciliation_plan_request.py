from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ReconciliationPlanRequest")



@_attrs_define
class ReconciliationPlanRequest:
    """
        Attributes:
            commit (str):
            profile_id (str):
     """

    commit: str
    profile_id: str





    def to_dict(self) -> dict[str, Any]:
        commit = self.commit

        profile_id = self.profile_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "commit": commit,
            "profile_id": profile_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        commit = d.pop("commit")

        profile_id = d.pop("profile_id")

        reconciliation_plan_request = cls(
            commit=commit,
            profile_id=profile_id,
        )

        return reconciliation_plan_request
