from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.update_recipe_draft_request_document import UpdateRecipeDraftRequestDocument





T = TypeVar("T", bound="UpdateRecipeDraftRequest")



@_attrs_define
class UpdateRecipeDraftRequest:
    """
        Attributes:
            document (UpdateRecipeDraftRequestDocument):
            expected_revision (int):
     """

    document: 'UpdateRecipeDraftRequestDocument'
    expected_revision: int





    def to_dict(self) -> dict[str, Any]:
        from ..models.update_recipe_draft_request_document import UpdateRecipeDraftRequestDocument
        document = self.document.to_dict()

        expected_revision = self.expected_revision


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "document": document,
            "expected_revision": expected_revision,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.update_recipe_draft_request_document import UpdateRecipeDraftRequestDocument
        d = dict(src_dict)
        document = UpdateRecipeDraftRequestDocument.from_dict(d.pop("document"))




        expected_revision = d.pop("expected_revision")

        update_recipe_draft_request = cls(
            document=document,
            expected_revision=expected_revision,
        )

        return update_recipe_draft_request
