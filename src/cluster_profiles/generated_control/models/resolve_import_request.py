from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.resolve_import_request_overlays import ResolveImportRequestOverlays





T = TypeVar("T", bound="ResolveImportRequest")



@_attrs_define
class ResolveImportRequest:
    """
        Attributes:
            expected_revision (int):
            overlays (ResolveImportRequestOverlays):
     """

    expected_revision: int
    overlays: 'ResolveImportRequestOverlays'





    def to_dict(self) -> dict[str, Any]:
        from ..models.resolve_import_request_overlays import ResolveImportRequestOverlays
        expected_revision = self.expected_revision

        overlays = self.overlays.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "expected_revision": expected_revision,
            "overlays": overlays,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.resolve_import_request_overlays import ResolveImportRequestOverlays
        d = dict(src_dict)
        expected_revision = d.pop("expected_revision")

        overlays = ResolveImportRequestOverlays.from_dict(d.pop("overlays"))




        resolve_import_request = cls(
            expected_revision=expected_revision,
            overlays=overlays,
        )

        return resolve_import_request
