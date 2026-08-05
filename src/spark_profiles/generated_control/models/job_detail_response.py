from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import cast, Union

if TYPE_CHECKING:
  from ..models.job_progress import JobProgress
  from ..models.job_operation_response import JobOperationResponse





T = TypeVar("T", bound="JobDetailResponse")



@_attrs_define
class JobDetailResponse:
    """
        Attributes:
            base_commit (str):
            current_attempt (int):
            id (str):
            kind (str):
            operations (list['JobOperationResponse']):
            progress (JobProgress):
            reconciliation_id (Union[None, str]):
            state (str):
            status_reason (Union[None, str]):
            targets (list[str]):
     """

    base_commit: str
    current_attempt: int
    id: str
    kind: str
    operations: list['JobOperationResponse']
    progress: 'JobProgress'
    reconciliation_id: Union[None, str]
    state: str
    status_reason: Union[None, str]
    targets: list[str]





    def to_dict(self) -> dict[str, Any]:
        from ..models.job_progress import JobProgress
        from ..models.job_operation_response import JobOperationResponse
        base_commit = self.base_commit

        current_attempt = self.current_attempt

        id = self.id

        kind = self.kind

        operations = []
        for operations_item_data in self.operations:
            operations_item = operations_item_data.to_dict()
            operations.append(operations_item)



        progress = self.progress.to_dict()

        reconciliation_id: Union[None, str]
        reconciliation_id = self.reconciliation_id

        state = self.state

        status_reason: Union[None, str]
        status_reason = self.status_reason

        targets = self.targets




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "base_commit": base_commit,
            "current_attempt": current_attempt,
            "id": id,
            "kind": kind,
            "operations": operations,
            "progress": progress,
            "reconciliation_id": reconciliation_id,
            "state": state,
            "status_reason": status_reason,
            "targets": targets,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.job_progress import JobProgress
        from ..models.job_operation_response import JobOperationResponse
        d = dict(src_dict)
        base_commit = d.pop("base_commit")

        current_attempt = d.pop("current_attempt")

        id = d.pop("id")

        kind = d.pop("kind")

        operations = []
        _operations = d.pop("operations")
        for operations_item_data in (_operations):
            operations_item = JobOperationResponse.from_dict(operations_item_data)



            operations.append(operations_item)


        progress = JobProgress.from_dict(d.pop("progress"))




        def _parse_reconciliation_id(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        reconciliation_id = _parse_reconciliation_id(d.pop("reconciliation_id"))


        state = d.pop("state")

        def _parse_status_reason(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        status_reason = _parse_status_reason(d.pop("status_reason"))


        targets = cast(list[str], d.pop("targets"))


        job_detail_response = cls(
            base_commit=base_commit,
            current_attempt=current_attempt,
            id=id,
            kind=kind,
            operations=operations,
            progress=progress,
            reconciliation_id=reconciliation_id,
            state=state,
            status_reason=status_reason,
            targets=targets,
        )

        return job_detail_response
