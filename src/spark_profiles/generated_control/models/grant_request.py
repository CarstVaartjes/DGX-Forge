from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="GrantRequest")



@_attrs_define
class GrantRequest:
    """
        Attributes:
            node_id (str):
            ttl_seconds (int):
     """

    node_id: str
    ttl_seconds: int





    def to_dict(self) -> dict[str, Any]:
        node_id = self.node_id

        ttl_seconds = self.ttl_seconds


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_id": node_id,
            "ttl_seconds": ttl_seconds,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        node_id = d.pop("node_id")

        ttl_seconds = d.pop("ttl_seconds")

        grant_request = cls(
            node_id=node_id,
            ttl_seconds=ttl_seconds,
        )

        return grant_request
