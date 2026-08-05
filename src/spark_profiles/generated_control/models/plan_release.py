from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.plan_workload_requests import PlanWorkloadRequests
  from ..models.plan_endpoint import PlanEndpoint
  from ..models.plan_release_request import PlanReleaseRequest





T = TypeVar("T", bound="PlanRelease")



@_attrs_define
class PlanRelease:
    """
        Attributes:
            definition_hash (str):
            endpoint (PlanEndpoint):
            manifest_path (str):
            manifest_sha256 (str):
            release_request (PlanReleaseRequest):
            workload_requests (PlanWorkloadRequests):
     """

    definition_hash: str
    endpoint: 'PlanEndpoint'
    manifest_path: str
    manifest_sha256: str
    release_request: 'PlanReleaseRequest'
    workload_requests: 'PlanWorkloadRequests'





    def to_dict(self) -> dict[str, Any]:
        from ..models.plan_workload_requests import PlanWorkloadRequests
        from ..models.plan_endpoint import PlanEndpoint
        from ..models.plan_release_request import PlanReleaseRequest
        definition_hash = self.definition_hash

        endpoint = self.endpoint.to_dict()

        manifest_path = self.manifest_path

        manifest_sha256 = self.manifest_sha256

        release_request = self.release_request.to_dict()

        workload_requests = self.workload_requests.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "definition_hash": definition_hash,
            "endpoint": endpoint,
            "manifest_path": manifest_path,
            "manifest_sha256": manifest_sha256,
            "release_request": release_request,
            "workload_requests": workload_requests,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.plan_workload_requests import PlanWorkloadRequests
        from ..models.plan_endpoint import PlanEndpoint
        from ..models.plan_release_request import PlanReleaseRequest
        d = dict(src_dict)
        definition_hash = d.pop("definition_hash")

        endpoint = PlanEndpoint.from_dict(d.pop("endpoint"))




        manifest_path = d.pop("manifest_path")

        manifest_sha256 = d.pop("manifest_sha256")

        release_request = PlanReleaseRequest.from_dict(d.pop("release_request"))




        workload_requests = PlanWorkloadRequests.from_dict(d.pop("workload_requests"))




        plan_release = cls(
            definition_hash=definition_hash,
            endpoint=endpoint,
            manifest_path=manifest_path,
            manifest_sha256=manifest_sha256,
            release_request=release_request,
            workload_requests=workload_requests,
        )

        return plan_release
