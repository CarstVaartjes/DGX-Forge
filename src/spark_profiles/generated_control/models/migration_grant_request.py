from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import Union






T = TypeVar("T", bound="MigrationGrantRequest")



@_attrs_define
class MigrationGrantRequest:
    """
        Attributes:
            ttl_seconds (Union[Unset, int]):  Default: 600.
     """

    ttl_seconds: Union[Unset, int] = 600





    def to_dict(self) -> dict[str, Any]:
        ttl_seconds = self.ttl_seconds


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if ttl_seconds is not UNSET:
            field_dict["ttl_seconds"] = ttl_seconds

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        ttl_seconds = d.pop("ttl_seconds", UNSET)

        migration_grant_request = cls(
            ttl_seconds=ttl_seconds,
        )

        return migration_grant_request
