from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="MappingResponse")



@_attrs_define
class MappingResponse:
    """
        Attributes:
            generation (int):
            mapping_id (str):
            placement_digest (str):
     """

    generation: int
    mapping_id: str
    placement_digest: str





    def to_dict(self) -> dict[str, Any]:
        generation = self.generation

        mapping_id = self.mapping_id

        placement_digest = self.placement_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "generation": generation,
            "mapping_id": mapping_id,
            "placement_digest": placement_digest,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        generation = d.pop("generation")

        mapping_id = d.pop("mapping_id")

        placement_digest = d.pop("placement_digest")

        mapping_response = cls(
            generation=generation,
            mapping_id=mapping_id,
            placement_digest=placement_digest,
        )

        return mapping_response
