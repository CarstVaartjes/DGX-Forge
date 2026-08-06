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
  from ..models.package_candidate_response import PackageCandidateResponse





T = TypeVar("T", bound="PackageCandidatesResponse")



@_attrs_define
class PackageCandidatesResponse:
    """
        Attributes:
            candidates (list['PackageCandidateResponse']):
            total (int):
            next_cursor (Union[None, Unset, str]):
     """

    candidates: list['PackageCandidateResponse']
    total: int
    next_cursor: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.package_candidate_response import PackageCandidateResponse
        candidates = []
        for candidates_item_data in self.candidates:
            candidates_item = candidates_item_data.to_dict()
            candidates.append(candidates_item)



        total = self.total

        next_cursor: Union[None, Unset, str]
        if isinstance(self.next_cursor, Unset):
            next_cursor = UNSET
        else:
            next_cursor = self.next_cursor


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "candidates": candidates,
            "total": total,
        })
        if next_cursor is not UNSET:
            field_dict["next_cursor"] = next_cursor

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.package_candidate_response import PackageCandidateResponse
        d = dict(src_dict)
        candidates = []
        _candidates = d.pop("candidates")
        for candidates_item_data in (_candidates):
            candidates_item = PackageCandidateResponse.from_dict(candidates_item_data)



            candidates.append(candidates_item)


        total = d.pop("total")

        def _parse_next_cursor(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        next_cursor = _parse_next_cursor(d.pop("next_cursor", UNSET))


        package_candidates_response = cls(
            candidates=candidates,
            total=total,
            next_cursor=next_cursor,
        )

        return package_candidates_response
