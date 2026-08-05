from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.enrollment_summary import EnrollmentSummary





T = TypeVar("T", bound="EnrollmentListResponse")



@_attrs_define
class EnrollmentListResponse:
    """
        Attributes:
            enrollments (list['EnrollmentSummary']):
            next_cursor (Union[None, Unset, str]):
     """

    enrollments: list['EnrollmentSummary']
    next_cursor: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.enrollment_summary import EnrollmentSummary
        enrollments = []
        for enrollments_item_data in self.enrollments:
            enrollments_item = enrollments_item_data.to_dict()
            enrollments.append(enrollments_item)



        next_cursor: Union[None, Unset, str]
        if isinstance(self.next_cursor, Unset):
            next_cursor = UNSET
        else:
            next_cursor = self.next_cursor


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "enrollments": enrollments,
        })
        if next_cursor is not UNSET:
            field_dict["next_cursor"] = next_cursor

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.enrollment_summary import EnrollmentSummary
        d = dict(src_dict)
        enrollments = []
        _enrollments = d.pop("enrollments")
        for enrollments_item_data in (_enrollments):
            enrollments_item = EnrollmentSummary.from_dict(enrollments_item_data)



            enrollments.append(enrollments_item)


        def _parse_next_cursor(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        next_cursor = _parse_next_cursor(d.pop("next_cursor", UNSET))


        enrollment_list_response = cls(
            enrollments=enrollments,
            next_cursor=next_cursor,
        )

        return enrollment_list_response
