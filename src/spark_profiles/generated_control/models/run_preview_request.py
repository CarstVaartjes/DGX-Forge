from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.placement_request import PlacementRequest





T = TypeVar("T", bound="RunPreviewRequest")



@_attrs_define
class RunPreviewRequest:
    """
        Attributes:
            installation_id (str):
            placements (list['PlacementRequest']):
     """

    installation_id: str
    placements: list['PlacementRequest']





    def to_dict(self) -> dict[str, Any]:
        from ..models.placement_request import PlacementRequest
        installation_id = self.installation_id

        placements = []
        for placements_item_data in self.placements:
            placements_item = placements_item_data.to_dict()
            placements.append(placements_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "installation_id": installation_id,
            "placements": placements,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.placement_request import PlacementRequest
        d = dict(src_dict)
        installation_id = d.pop("installation_id")

        placements = []
        _placements = d.pop("placements")
        for placements_item_data in (_placements):
            placements_item = PlacementRequest.from_dict(placements_item_data)



            placements.append(placements_item)


        run_preview_request = cls(
            installation_id=installation_id,
            placements=placements,
        )

        return run_preview_request
