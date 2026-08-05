from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="ReconciliationAcceptedResponse")



@_attrs_define
class ReconciliationAcceptedResponse:
    """
        Attributes:
            base_commit (str):
            job_id (str):
            state (str):
            reconciliation_id (Union[None, Unset, str]):
     """

    base_commit: str
    job_id: str
    state: str
    reconciliation_id: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        base_commit = self.base_commit

        job_id = self.job_id

        state = self.state

        reconciliation_id: Union[None, Unset, str]
        if isinstance(self.reconciliation_id, Unset):
            reconciliation_id = UNSET
        else:
            reconciliation_id = self.reconciliation_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "base_commit": base_commit,
            "job_id": job_id,
            "state": state,
        })
        if reconciliation_id is not UNSET:
            field_dict["reconciliation_id"] = reconciliation_id

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        base_commit = d.pop("base_commit")

        job_id = d.pop("job_id")

        state = d.pop("state")

        def _parse_reconciliation_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        reconciliation_id = _parse_reconciliation_id(d.pop("reconciliation_id", UNSET))


        reconciliation_accepted_response = cls(
            base_commit=base_commit,
            job_id=job_id,
            state=state,
            reconciliation_id=reconciliation_id,
        )

        return reconciliation_accepted_response
