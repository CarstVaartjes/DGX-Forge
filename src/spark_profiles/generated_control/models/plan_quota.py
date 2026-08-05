from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="PlanQuota")



@_attrs_define
class PlanQuota:
    """
        Attributes:
            requests_per_minute (int):
            tokens_per_minute (int):
     """

    requests_per_minute: int
    tokens_per_minute: int





    def to_dict(self) -> dict[str, Any]:
        requests_per_minute = self.requests_per_minute

        tokens_per_minute = self.tokens_per_minute


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "requests_per_minute": requests_per_minute,
            "tokens_per_minute": tokens_per_minute,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        requests_per_minute = d.pop("requests_per_minute")

        tokens_per_minute = d.pop("tokens_per_minute")

        plan_quota = cls(
            requests_per_minute=requests_per_minute,
            tokens_per_minute=tokens_per_minute,
        )

        return plan_quota
