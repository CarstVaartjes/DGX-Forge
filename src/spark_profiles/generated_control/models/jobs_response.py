from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.job_summary import JobSummary





T = TypeVar("T", bound="JobsResponse")



@_attrs_define
class JobsResponse:
    """
        Attributes:
            jobs (list['JobSummary']):
     """

    jobs: list['JobSummary']





    def to_dict(self) -> dict[str, Any]:
        from ..models.job_summary import JobSummary
        jobs = []
        for jobs_item_data in self.jobs:
            jobs_item = jobs_item_data.to_dict()
            jobs.append(jobs_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "jobs": jobs,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.job_summary import JobSummary
        d = dict(src_dict)
        jobs = []
        _jobs = d.pop("jobs")
        for jobs_item_data in (_jobs):
            jobs_item = JobSummary.from_dict(jobs_item_data)



            jobs.append(jobs_item)


        jobs_response = cls(
            jobs=jobs,
        )

        return jobs_response
