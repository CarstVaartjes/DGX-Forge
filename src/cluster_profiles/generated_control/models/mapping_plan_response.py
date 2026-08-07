from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.mapping_plan_response_parameters import MappingPlanResponseParameters
  from ..models.mapping_node_plan_response import MappingNodePlanResponse





T = TypeVar("T", bound="MappingPlanResponse")



@_attrs_define
class MappingPlanResponse:
    """
        Attributes:
            generation (int):
            nodes (list['MappingNodePlanResponse']):
            parameters (MappingPlanResponseParameters):
            placement_digest (str):
            profile_name (str):
            recipe_content_sha256 (str):
            recipe_revision_id (str):
     """

    generation: int
    nodes: list['MappingNodePlanResponse']
    parameters: 'MappingPlanResponseParameters'
    placement_digest: str
    profile_name: str
    recipe_content_sha256: str
    recipe_revision_id: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.mapping_plan_response_parameters import MappingPlanResponseParameters
        from ..models.mapping_node_plan_response import MappingNodePlanResponse
        generation = self.generation

        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)



        parameters = self.parameters.to_dict()

        placement_digest = self.placement_digest

        profile_name = self.profile_name

        recipe_content_sha256 = self.recipe_content_sha256

        recipe_revision_id = self.recipe_revision_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "generation": generation,
            "nodes": nodes,
            "parameters": parameters,
            "placement_digest": placement_digest,
            "profile_name": profile_name,
            "recipe_content_sha256": recipe_content_sha256,
            "recipe_revision_id": recipe_revision_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.mapping_plan_response_parameters import MappingPlanResponseParameters
        from ..models.mapping_node_plan_response import MappingNodePlanResponse
        d = dict(src_dict)
        generation = d.pop("generation")

        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = MappingNodePlanResponse.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        parameters = MappingPlanResponseParameters.from_dict(d.pop("parameters"))




        placement_digest = d.pop("placement_digest")

        profile_name = d.pop("profile_name")

        recipe_content_sha256 = d.pop("recipe_content_sha256")

        recipe_revision_id = d.pop("recipe_revision_id")

        mapping_plan_response = cls(
            generation=generation,
            nodes=nodes,
            parameters=parameters,
            placement_digest=placement_digest,
            profile_name=profile_name,
            recipe_content_sha256=recipe_content_sha256,
            recipe_revision_id=recipe_revision_id,
        )

        return mapping_plan_response
