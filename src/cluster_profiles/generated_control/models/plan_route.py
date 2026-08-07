from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.plan_route_scheme import check_plan_route_scheme
from ..models.plan_route_scheme import PlanRouteScheme
from typing import cast

if TYPE_CHECKING:
  from ..models.plan_quota import PlanQuota





T = TypeVar("T", bound="PlanRoute")



@_attrs_define
class PlanRoute:
    """
        Attributes:
            entrypoint_node_id (str):
            nodes (list[str]):
            path (str):
            port (int):
            quota (PlanQuota):
            quota_digest (str):
            scheme (PlanRouteScheme):
            workload_id (str):
     """

    entrypoint_node_id: str
    nodes: list[str]
    path: str
    port: int
    quota: 'PlanQuota'
    quota_digest: str
    scheme: PlanRouteScheme
    workload_id: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.plan_quota import PlanQuota
        entrypoint_node_id = self.entrypoint_node_id

        nodes = self.nodes



        path = self.path

        port = self.port

        quota = self.quota.to_dict()

        quota_digest = self.quota_digest

        scheme: str = self.scheme

        workload_id = self.workload_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "entrypoint_node_id": entrypoint_node_id,
            "nodes": nodes,
            "path": path,
            "port": port,
            "quota": quota,
            "quota_digest": quota_digest,
            "scheme": scheme,
            "workload_id": workload_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.plan_quota import PlanQuota
        d = dict(src_dict)
        entrypoint_node_id = d.pop("entrypoint_node_id")

        nodes = cast(list[str], d.pop("nodes"))


        path = d.pop("path")

        port = d.pop("port")

        quota = PlanQuota.from_dict(d.pop("quota"))




        quota_digest = d.pop("quota_digest")

        scheme = check_plan_route_scheme(d.pop("scheme"))




        workload_id = d.pop("workload_id")

        plan_route = cls(
            entrypoint_node_id=entrypoint_node_id,
            nodes=nodes,
            path=path,
            port=port,
            quota=quota,
            quota_digest=quota_digest,
            scheme=scheme,
            workload_id=workload_id,
        )

        return plan_route
