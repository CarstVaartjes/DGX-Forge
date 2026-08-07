from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.run_node_plan_response import RunNodePlanResponse





T = TypeVar("T", bound="RunPlanResponse")



@_attrs_define
class RunPlanResponse:
    """
        Attributes:
            allowed (bool):
            installation_id (str):
            nodes (list['RunNodePlanResponse']):
            plan_digest (str):
            recipe_revision_id (str):
     """

    allowed: bool
    installation_id: str
    nodes: list['RunNodePlanResponse']
    plan_digest: str
    recipe_revision_id: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_node_plan_response import RunNodePlanResponse
        allowed = self.allowed

        installation_id = self.installation_id

        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)



        plan_digest = self.plan_digest

        recipe_revision_id = self.recipe_revision_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "allowed": allowed,
            "installation_id": installation_id,
            "nodes": nodes,
            "plan_digest": plan_digest,
            "recipe_revision_id": recipe_revision_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_node_plan_response import RunNodePlanResponse
        d = dict(src_dict)
        allowed = d.pop("allowed")

        installation_id = d.pop("installation_id")

        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = RunNodePlanResponse.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        plan_digest = d.pop("plan_digest")

        recipe_revision_id = d.pop("recipe_revision_id")

        run_plan_response = cls(
            allowed=allowed,
            installation_id=installation_id,
            nodes=nodes,
            plan_digest=plan_digest,
            recipe_revision_id=recipe_revision_id,
        )

        return run_plan_response
