from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ApplyRequest")



@_attrs_define
class ApplyRequest:
    """
        Attributes:
            report_digest (str):
            source_sha256 (str):
            source_yaml (str):
     """

    report_digest: str
    source_sha256: str
    source_yaml: str





    def to_dict(self) -> dict[str, Any]:
        report_digest = self.report_digest

        source_sha256 = self.source_sha256

        source_yaml = self.source_yaml


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "report_digest": report_digest,
            "source_sha256": source_sha256,
            "source_yaml": source_yaml,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        report_digest = d.pop("report_digest")

        source_sha256 = d.pop("source_sha256")

        source_yaml = d.pop("source_yaml")

        apply_request = cls(
            report_digest=report_digest,
            source_sha256=source_sha256,
            source_yaml=source_yaml,
        )

        return apply_request
