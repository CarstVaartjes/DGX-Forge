from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.job_operation_progress import JobOperationProgress





T = TypeVar("T", bound="JobOperationResponse")



@_attrs_define
class JobOperationResponse:
    """
        Attributes:
            attempt (int):
            id (str):
            kind (str):
            node_id (str):
            state (str):
            graph_operation_id (Union[None, Unset, str]):
            progress (Union['JobOperationProgress', None, Unset]):
            updated_at (Union[None, Unset, str]):
     """

    attempt: int
    id: str
    kind: str
    node_id: str
    state: str
    graph_operation_id: Union[None, Unset, str] = UNSET
    progress: Union['JobOperationProgress', None, Unset] = UNSET
    updated_at: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.job_operation_progress import JobOperationProgress
        attempt = self.attempt

        id = self.id

        kind = self.kind

        node_id = self.node_id

        state = self.state

        graph_operation_id: Union[None, Unset, str]
        if isinstance(self.graph_operation_id, Unset):
            graph_operation_id = UNSET
        else:
            graph_operation_id = self.graph_operation_id

        progress: Union[None, Unset, dict[str, Any]]
        if isinstance(self.progress, Unset):
            progress = UNSET
        elif isinstance(self.progress, JobOperationProgress):
            progress = self.progress.to_dict()
        else:
            progress = self.progress

        updated_at: Union[None, Unset, str]
        if isinstance(self.updated_at, Unset):
            updated_at = UNSET
        else:
            updated_at = self.updated_at


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "attempt": attempt,
            "id": id,
            "kind": kind,
            "node_id": node_id,
            "state": state,
        })
        if graph_operation_id is not UNSET:
            field_dict["graph_operation_id"] = graph_operation_id
        if progress is not UNSET:
            field_dict["progress"] = progress
        if updated_at is not UNSET:
            field_dict["updated_at"] = updated_at

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.job_operation_progress import JobOperationProgress
        d = dict(src_dict)
        attempt = d.pop("attempt")

        id = d.pop("id")

        kind = d.pop("kind")

        node_id = d.pop("node_id")

        state = d.pop("state")

        def _parse_graph_operation_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        graph_operation_id = _parse_graph_operation_id(d.pop("graph_operation_id", UNSET))


        def _parse_progress(data: object) -> Union['JobOperationProgress', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                progress_type_0 = JobOperationProgress.from_dict(data)



                return progress_type_0
            except: # noqa: E722
                pass
            return cast(Union['JobOperationProgress', None, Unset], data)

        progress = _parse_progress(d.pop("progress", UNSET))


        def _parse_updated_at(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        updated_at = _parse_updated_at(d.pop("updated_at", UNSET))


        job_operation_response = cls(
            attempt=attempt,
            id=id,
            kind=kind,
            node_id=node_id,
            state=state,
            graph_operation_id=graph_operation_id,
            progress=progress,
            updated_at=updated_at,
        )

        return job_operation_response
