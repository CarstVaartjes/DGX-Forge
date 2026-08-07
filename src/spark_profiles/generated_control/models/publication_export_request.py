from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="PublicationExportRequest")



@_attrs_define
class PublicationExportRequest:
    """
        Attributes:
            publisher (str):
     """

    publisher: str





    def to_dict(self) -> dict[str, Any]:
        publisher = self.publisher


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "publisher": publisher,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        publisher = d.pop("publisher")

        publication_export_request = cls(
            publisher=publisher,
        )

        return publication_export_request
