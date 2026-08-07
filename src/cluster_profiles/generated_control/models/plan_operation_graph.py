from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.plan_operation import PlanOperation





T = TypeVar("T", bound="PlanOperationGraph")



@_attrs_define
class PlanOperationGraph:
    """
        Attributes:
            base_commit (str):
            nodes (list['PlanOperation']):
            schema_version (Literal[1]):
            targets (list[str]):
     """

    base_commit: str
    nodes: list['PlanOperation']
    schema_version: Literal[1]
    targets: list[str]





    def to_dict(self) -> dict[str, Any]:
        from ..models.plan_operation import PlanOperation
        base_commit = self.base_commit

        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)



        schema_version = self.schema_version

        targets = self.targets




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "base_commit": base_commit,
            "nodes": nodes,
            "schema_version": schema_version,
            "targets": targets,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.plan_operation import PlanOperation
        d = dict(src_dict)
        base_commit = d.pop("base_commit")

        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = PlanOperation.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        targets = cast(list[str], d.pop("targets"))


        plan_operation_graph = cls(
            base_commit=base_commit,
            nodes=nodes,
            schema_version=schema_version,
            targets=targets,
        )

        return plan_operation_graph
