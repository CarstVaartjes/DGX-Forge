from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.test_report_request_report import TestReportRequestReport





T = TypeVar("T", bound="TestReportRequest")



@_attrs_define
class TestReportRequest:
    """
        Attributes:
            report (TestReportRequestReport):
     """

    report: 'TestReportRequestReport'





    def to_dict(self) -> dict[str, Any]:
        from ..models.test_report_request_report import TestReportRequestReport
        report = self.report.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "report": report,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.test_report_request_report import TestReportRequestReport
        d = dict(src_dict)
        report = TestReportRequestReport.from_dict(d.pop("report"))




        test_report_request = cls(
            report=report,
        )

        return test_report_request
