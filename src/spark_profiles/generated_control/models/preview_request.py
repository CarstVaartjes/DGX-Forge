from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="PreviewRequest")



@_attrs_define
class PreviewRequest:
    """
        Attributes:
            source_yaml (str):
     """

    source_yaml: str





    def to_dict(self) -> dict[str, Any]:
        source_yaml = self.source_yaml


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "source_yaml": source_yaml,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        source_yaml = d.pop("source_yaml")

        preview_request = cls(
            source_yaml=source_yaml,
        )

        return preview_request
