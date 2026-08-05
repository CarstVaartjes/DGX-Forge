from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.reconciliation_plan_response_releases import ReconciliationPlanResponseReleases
  from ..models.reconciliation_plan_response_routes import ReconciliationPlanResponseRoutes
  from ..models.reconciliation_plan_response_placements import ReconciliationPlanResponsePlacements
  from ..models.reconciliation_plan_response_operation_graph import ReconciliationPlanResponseOperationGraph
  from ..models.reconciliation_plan_response_input_digests import ReconciliationPlanResponseInputDigests





T = TypeVar("T", bound="ReconciliationPlanResponse")



@_attrs_define
class ReconciliationPlanResponse:
    """
        Attributes:
            agent_protocol_range (list[int]):
            commit (str):
            digest (str):
            input_digests (ReconciliationPlanResponseInputDigests):
            operation_graph (ReconciliationPlanResponseOperationGraph):
            placements (ReconciliationPlanResponsePlacements):
            reconciliation_id (str):
            releases (ReconciliationPlanResponseReleases):
            routes (ReconciliationPlanResponseRoutes):
            targets (list[str]):
     """

    agent_protocol_range: list[int]
    commit: str
    digest: str
    input_digests: 'ReconciliationPlanResponseInputDigests'
    operation_graph: 'ReconciliationPlanResponseOperationGraph'
    placements: 'ReconciliationPlanResponsePlacements'
    reconciliation_id: str
    releases: 'ReconciliationPlanResponseReleases'
    routes: 'ReconciliationPlanResponseRoutes'
    targets: list[str]





    def to_dict(self) -> dict[str, Any]:
        from ..models.reconciliation_plan_response_releases import ReconciliationPlanResponseReleases
        from ..models.reconciliation_plan_response_routes import ReconciliationPlanResponseRoutes
        from ..models.reconciliation_plan_response_placements import ReconciliationPlanResponsePlacements
        from ..models.reconciliation_plan_response_operation_graph import ReconciliationPlanResponseOperationGraph
        from ..models.reconciliation_plan_response_input_digests import ReconciliationPlanResponseInputDigests
        agent_protocol_range = self.agent_protocol_range



        commit = self.commit

        digest = self.digest

        input_digests = self.input_digests.to_dict()

        operation_graph = self.operation_graph.to_dict()

        placements = self.placements.to_dict()

        reconciliation_id = self.reconciliation_id

        releases = self.releases.to_dict()

        routes = self.routes.to_dict()

        targets = self.targets




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "agent_protocol_range": agent_protocol_range,
            "commit": commit,
            "digest": digest,
            "input_digests": input_digests,
            "operation_graph": operation_graph,
            "placements": placements,
            "reconciliation_id": reconciliation_id,
            "releases": releases,
            "routes": routes,
            "targets": targets,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.reconciliation_plan_response_releases import ReconciliationPlanResponseReleases
        from ..models.reconciliation_plan_response_routes import ReconciliationPlanResponseRoutes
        from ..models.reconciliation_plan_response_placements import ReconciliationPlanResponsePlacements
        from ..models.reconciliation_plan_response_operation_graph import ReconciliationPlanResponseOperationGraph
        from ..models.reconciliation_plan_response_input_digests import ReconciliationPlanResponseInputDigests
        d = dict(src_dict)
        agent_protocol_range = cast(list[int], d.pop("agent_protocol_range"))


        commit = d.pop("commit")

        digest = d.pop("digest")

        input_digests = ReconciliationPlanResponseInputDigests.from_dict(d.pop("input_digests"))




        operation_graph = ReconciliationPlanResponseOperationGraph.from_dict(d.pop("operation_graph"))




        placements = ReconciliationPlanResponsePlacements.from_dict(d.pop("placements"))




        reconciliation_id = d.pop("reconciliation_id")

        releases = ReconciliationPlanResponseReleases.from_dict(d.pop("releases"))




        routes = ReconciliationPlanResponseRoutes.from_dict(d.pop("routes"))




        targets = cast(list[str], d.pop("targets"))


        reconciliation_plan_response = cls(
            agent_protocol_range=agent_protocol_range,
            commit=commit,
            digest=digest,
            input_digests=input_digests,
            operation_graph=operation_graph,
            placements=placements,
            reconciliation_id=reconciliation_id,
            releases=releases,
            routes=routes,
            targets=targets,
        )

        return reconciliation_plan_response
