from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.agent_summary import AgentSummary





T = TypeVar("T", bound="AgentsResponse")



@_attrs_define
class AgentsResponse:
    """
        Attributes:
            agents (list['AgentSummary']):
     """

    agents: list['AgentSummary']





    def to_dict(self) -> dict[str, Any]:
        from ..models.agent_summary import AgentSummary
        agents = []
        for agents_item_data in self.agents:
            agents_item = agents_item_data.to_dict()
            agents.append(agents_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "agents": agents,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_summary import AgentSummary
        d = dict(src_dict)
        agents = []
        _agents = d.pop("agents")
        for agents_item_data in (_agents):
            agents_item = AgentSummary.from_dict(agents_item_data)



            agents.append(agents_item)


        agents_response = cls(
            agents=agents,
        )

        return agents_response
