from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="PackageComponentResponse")



@_attrs_define
class PackageComponentResponse:
    """
        Attributes:
            digest (str):
            kind (str):
            name (str):
     """

    digest: str
    kind: str
    name: str





    def to_dict(self) -> dict[str, Any]:
        digest = self.digest

        kind = self.kind

        name = self.name


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "digest": digest,
            "kind": kind,
            "name": name,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        digest = d.pop("digest")

        kind = d.pop("kind")

        name = d.pop("name")

        package_component_response = cls(
            digest=digest,
            kind=kind,
            name=name,
        )

        return package_component_response
