from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="GlobalImportPreviewRequest")



@_attrs_define
class GlobalImportPreviewRequest:
    """
        Attributes:
            uri (str):
     """

    uri: str





    def to_dict(self) -> dict[str, Any]:
        uri = self.uri


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "uri": uri,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        uri = d.pop("uri")

        global_import_preview_request = cls(
            uri=uri,
        )

        return global_import_preview_request
