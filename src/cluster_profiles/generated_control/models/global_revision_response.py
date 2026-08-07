from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.global_revision_response_document import GlobalRevisionResponseDocument





T = TypeVar("T", bound="GlobalRevisionResponse")



@_attrs_define
class GlobalRevisionResponse:
    """
        Attributes:
            content_sha256 (str):
            document (GlobalRevisionResponseDocument):
            published_at (str):
            publisher (str):
            recipe_id (str):
            revision_id (str):
            revision_number (int):
            slug (str):
     """

    content_sha256: str
    document: 'GlobalRevisionResponseDocument'
    published_at: str
    publisher: str
    recipe_id: str
    revision_id: str
    revision_number: int
    slug: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.global_revision_response_document import GlobalRevisionResponseDocument
        content_sha256 = self.content_sha256

        document = self.document.to_dict()

        published_at = self.published_at

        publisher = self.publisher

        recipe_id = self.recipe_id

        revision_id = self.revision_id

        revision_number = self.revision_number

        slug = self.slug


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "content_sha256": content_sha256,
            "document": document,
            "published_at": published_at,
            "publisher": publisher,
            "recipe_id": recipe_id,
            "revision_id": revision_id,
            "revision_number": revision_number,
            "slug": slug,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.global_revision_response_document import GlobalRevisionResponseDocument
        d = dict(src_dict)
        content_sha256 = d.pop("content_sha256")

        document = GlobalRevisionResponseDocument.from_dict(d.pop("document"))




        published_at = d.pop("published_at")

        publisher = d.pop("publisher")

        recipe_id = d.pop("recipe_id")

        revision_id = d.pop("revision_id")

        revision_number = d.pop("revision_number")

        slug = d.pop("slug")

        global_revision_response = cls(
            content_sha256=content_sha256,
            document=document,
            published_at=published_at,
            publisher=publisher,
            recipe_id=recipe_id,
            revision_id=revision_id,
            revision_number=revision_number,
            slug=slug,
        )

        return global_revision_response
