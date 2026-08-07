from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.create_recipe_request_document import CreateRecipeRequestDocument





T = TypeVar("T", bound="CreateRecipeRequest")



@_attrs_define
class CreateRecipeRequest:
    """
        Attributes:
            document (CreateRecipeRequestDocument):
            slug (str):
     """

    document: 'CreateRecipeRequestDocument'
    slug: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.create_recipe_request_document import CreateRecipeRequestDocument
        document = self.document.to_dict()

        slug = self.slug


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "document": document,
            "slug": slug,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.create_recipe_request_document import CreateRecipeRequestDocument
        d = dict(src_dict)
        document = CreateRecipeRequestDocument.from_dict(d.pop("document"))




        slug = d.pop("slug")

        create_recipe_request = cls(
            document=document,
            slug=slug,
        )

        return create_recipe_request
