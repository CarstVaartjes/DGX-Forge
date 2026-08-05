from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.enrollment_decision_response_state import check_enrollment_decision_response_state
from ..models.enrollment_decision_response_state import EnrollmentDecisionResponseState
from typing import cast






T = TypeVar("T", bound="EnrollmentDecisionResponse")



@_attrs_define
class EnrollmentDecisionResponse:
    """
        Attributes:
            id (str):
            node_id (str):
            state (EnrollmentDecisionResponseState):
     """

    id: str
    node_id: str
    state: EnrollmentDecisionResponseState





    def to_dict(self) -> dict[str, Any]:
        id = self.id

        node_id = self.node_id

        state: str = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "id": id,
            "node_id": node_id,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        node_id = d.pop("node_id")

        state = check_enrollment_decision_response_state(d.pop("state"))




        enrollment_decision_response = cls(
            id=id,
            node_id=node_id,
            state=state,
        )

        return enrollment_decision_response
