from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="PackagePromotionRequest")



@_attrs_define
class PackagePromotionRequest:
    """
        Attributes:
            preview_digest (str):
     """

    preview_digest: str





    def to_dict(self) -> dict[str, Any]:
        preview_digest = self.preview_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "preview_digest": preview_digest,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        preview_digest = d.pop("preview_digest")

        package_promotion_request = cls(
            preview_digest=preview_digest,
        )

        return package_promotion_request
