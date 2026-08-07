from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="JobOperationProgress")



@_attrs_define
class JobOperationProgress:
    """
        Attributes:
            phase (str):
     """

    phase: str





    def to_dict(self) -> dict[str, Any]:
        phase = self.phase


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "phase": phase,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        phase = d.pop("phase")

        job_operation_progress = cls(
            phase=phase,
        )

        return job_operation_progress
