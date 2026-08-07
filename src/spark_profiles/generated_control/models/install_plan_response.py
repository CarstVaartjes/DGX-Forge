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
            image_digest (str):
            mapping_generation (int):
            mapping_id (str):
            nodes (list['InstallNodePlanResponse']):
            plan_digest (str):
            recipe_build_id (str):
            recipe_content_sha256 (str):
            recipe_revision_id (str):
     """

    allowed: bool
    image_digest: str
    mapping_generation: int
    mapping_id: str
    nodes: list['InstallNodePlanResponse']
    plan_digest: str
    recipe_build_id: str
    recipe_content_sha256: str
    recipe_revision_id: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.install_node_plan_response import InstallNodePlanResponse
        allowed = self.allowed

        image_digest = self.image_digest

        mapping_generation = self.mapping_generation

        mapping_id = self.mapping_id

        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)



        plan_digest = self.plan_digest

        recipe_build_id = self.recipe_build_id

        recipe_content_sha256 = self.recipe_content_sha256

        recipe_revision_id = self.recipe_revision_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "allowed": allowed,
            "image_digest": image_digest,
            "mapping_generation": mapping_generation,
            "mapping_id": mapping_id,
            "nodes": nodes,
            "plan_digest": plan_digest,
            "recipe_build_id": recipe_build_id,
            "recipe_content_sha256": recipe_content_sha256,
            "recipe_revision_id": recipe_revision_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.install_node_plan_response import InstallNodePlanResponse
        d = dict(src_dict)
        allowed = d.pop("allowed")

        image_digest = d.pop("image_digest")

        mapping_generation = d.pop("mapping_generation")

        mapping_id = d.pop("mapping_id")

        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = InstallNodePlanResponse.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        plan_digest = d.pop("plan_digest")

        recipe_build_id = d.pop("recipe_build_id")

        recipe_content_sha256 = d.pop("recipe_content_sha256")

        recipe_revision_id = d.pop("recipe_revision_id")

        install_plan_response = cls(
            allowed=allowed,
            image_digest=image_digest,
            mapping_generation=mapping_generation,
            mapping_id=mapping_id,
            nodes=nodes,
            plan_digest=plan_digest,
            recipe_build_id=recipe_build_id,
            recipe_content_sha256=recipe_content_sha256,
            recipe_revision_id=recipe_revision_id,
        )

        return install_plan_response
