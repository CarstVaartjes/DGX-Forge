from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="UpdatePlanRequest")



@_attrs_define
class UpdatePlanRequest:
    """
        Attributes:
            release (str):
     """

    release: str





    def to_dict(self) -> dict[str, Any]:
        release = self.release


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "release": release,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        release = d.pop("release")

        update_plan_request = cls(
            release=release,
        )

        return update_plan_request
