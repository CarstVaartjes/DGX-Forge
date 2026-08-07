from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="BoundedErrorResponse")



@_attrs_define
class BoundedErrorResponse:
    """
        Attributes:
            detail (str):
     """

    detail: str





    def to_dict(self) -> dict[str, Any]:
        detail = self.detail


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "detail": detail,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        detail = d.pop("detail")

        bounded_error_response = cls(
            detail=detail,
        )

        return bounded_error_response
