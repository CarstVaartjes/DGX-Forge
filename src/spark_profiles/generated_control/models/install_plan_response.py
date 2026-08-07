from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.install_node_plan_response import InstallNodePlanResponse





T = TypeVar("T", bound="InstallPlanResponse")



@_attrs_define
class InstallPlanResponse:
    """
        Attributes:
            allowed (bool):
            nodes (list['InstallNodePlanResponse']):
            plan_digest (str):
            recipe_content_sha256 (str):
            recipe_revision_id (str):
     """

    allowed: bool
    nodes: list['InstallNodePlanResponse']
    plan_digest: str
    recipe_content_sha256: str
    recipe_revision_id: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.install_node_plan_response import InstallNodePlanResponse
        allowed = self.allowed

        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)



        plan_digest = self.plan_digest

        recipe_content_sha256 = self.recipe_content_sha256

        recipe_revision_id = self.recipe_revision_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "allowed": allowed,
            "nodes": nodes,
            "plan_digest": plan_digest,
            "recipe_content_sha256": recipe_content_sha256,
            "recipe_revision_id": recipe_revision_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.install_node_plan_response import InstallNodePlanResponse
        d = dict(src_dict)
        allowed = d.pop("allowed")

        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = InstallNodePlanResponse.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        plan_digest = d.pop("plan_digest")

        recipe_content_sha256 = d.pop("recipe_content_sha256")

        recipe_revision_id = d.pop("recipe_revision_id")

        install_plan_response = cls(
            allowed=allowed,
            nodes=nodes,
            plan_digest=plan_digest,
            recipe_content_sha256=recipe_content_sha256,
            recipe_revision_id=recipe_revision_id,
        )

        return install_plan_response
