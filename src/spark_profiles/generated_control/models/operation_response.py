from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import cast, Union

if TYPE_CHECKING:
  from ..models.operation_response_result_type_0 import OperationResponseResultType0





T = TypeVar("T", bound="OperationResponse")



@_attrs_define
class OperationResponse:
    """
        Attributes:
            id (str):
            kind (str):
            nodes (list[str]):
            owner_id (str):
            plan_digest (str):
            result (Union['OperationResponseResultType0', None]):
            state (str):
     """

    id: str
    kind: str
    nodes: list[str]
    owner_id: str
    plan_digest: str
    result: Union['OperationResponseResultType0', None]
    state: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.operation_response_result_type_0 import OperationResponseResultType0
        id = self.id

        kind = self.kind

        nodes = self.nodes



        owner_id = self.owner_id

        plan_digest = self.plan_digest

        result: Union[None, dict[str, Any]]
        if isinstance(self.result, OperationResponseResultType0):
            result = self.result.to_dict()
        else:
            result = self.result

        state = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "id": id,
            "kind": kind,
            "nodes": nodes,
            "owner_id": owner_id,
            "plan_digest": plan_digest,
            "result": result,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.operation_response_result_type_0 import OperationResponseResultType0
        d = dict(src_dict)
        id = d.pop("id")

        kind = d.pop("kind")

        nodes = cast(list[str], d.pop("nodes"))


        owner_id = d.pop("owner_id")

        plan_digest = d.pop("plan_digest")

        def _parse_result(data: object) -> Union['OperationResponseResultType0', None]:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = OperationResponseResultType0.from_dict(data)



                return result_type_0
            except: # noqa: E722
                pass
            return cast(Union['OperationResponseResultType0', None], data)

        result = _parse_result(d.pop("result"))


        state = d.pop("state")

        operation_response = cls(
            id=id,
            kind=kind,
            nodes=nodes,
            owner_id=owner_id,
            plan_digest=plan_digest,
            result=result,
            state=state,
        )

        return operation_response
