from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.plan_prepare_request import PlanPrepareRequest
  from ..models.plan_workload_request import PlanWorkloadRequest
  from ..models.plan_start_request import PlanStartRequest
  from ..models.plan_verify_request import PlanVerifyRequest





T = TypeVar("T", bound="PlanWorkloadRequests")



@_attrs_define
class PlanWorkloadRequests:
    """
        Attributes:
            health (PlanWorkloadRequest):
            prepare (PlanPrepareRequest):
            start (PlanStartRequest):
            stop (PlanWorkloadRequest):
            verify (PlanVerifyRequest):
     """

    health: 'PlanWorkloadRequest'
    prepare: 'PlanPrepareRequest'
    start: 'PlanStartRequest'
    stop: 'PlanWorkloadRequest'
    verify: 'PlanVerifyRequest'





    def to_dict(self) -> dict[str, Any]:
        from ..models.plan_prepare_request import PlanPrepareRequest
        from ..models.plan_workload_request import PlanWorkloadRequest
        from ..models.plan_start_request import PlanStartRequest
        from ..models.plan_verify_request import PlanVerifyRequest
        health = self.health.to_dict()

        prepare = self.prepare.to_dict()

        start = self.start.to_dict()

        stop = self.stop.to_dict()

        verify = self.verify.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "health": health,
            "prepare": prepare,
            "start": start,
            "stop": stop,
            "verify": verify,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.plan_prepare_request import PlanPrepareRequest
        from ..models.plan_workload_request import PlanWorkloadRequest
        from ..models.plan_start_request import PlanStartRequest
        from ..models.plan_verify_request import PlanVerifyRequest
        d = dict(src_dict)
        health = PlanWorkloadRequest.from_dict(d.pop("health"))




        prepare = PlanPrepareRequest.from_dict(d.pop("prepare"))




        start = PlanStartRequest.from_dict(d.pop("start"))




        stop = PlanWorkloadRequest.from_dict(d.pop("stop"))




        verify = PlanVerifyRequest.from_dict(d.pop("verify"))




        plan_workload_requests = cls(
            health=health,
            prepare=prepare,
            start=start,
            stop=stop,
            verify=verify,
        )

        return plan_workload_requests
