from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="PackageProgress")



@_attrs_define
class PackageProgress:
    """
        Attributes:
            completed (int):
            failed (int):
            running (int):
            total (int):
     """

    completed: int
    failed: int
    running: int
    total: int





    def to_dict(self) -> dict[str, Any]:
        completed = self.completed

        failed = self.failed

        running = self.running

        total = self.total


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "completed": completed,
            "failed": failed,
            "running": running,
            "total": total,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        completed = d.pop("completed")

        failed = d.pop("failed")

        running = d.pop("running")

        total = d.pop("total")

        package_progress = cls(
            completed=completed,
            failed=failed,
            running=running,
            total=total,
        )

        return package_progress
