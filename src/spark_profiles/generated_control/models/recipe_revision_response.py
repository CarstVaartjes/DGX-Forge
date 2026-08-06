from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_revision_response_lifecycle import check_recipe_revision_response_lifecycle
from ..models.recipe_revision_response_lifecycle import RecipeRevisionResponseLifecycle
from ..models.recipe_revision_response_origin import check_recipe_revision_response_origin
from ..models.recipe_revision_response_origin import RecipeRevisionResponseOrigin
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.recipe_revision_response_document import RecipeRevisionResponseDocument





T = TypeVar("T", bound="RecipeRevisionResponse")



@_attrs_define
class RecipeRevisionResponse:
    """
        Attributes:
            created_at (str):
            created_by (str):
            description (str):
            document (RecipeRevisionResponseDocument):
            id (str):
            lifecycle (RecipeRevisionResponseLifecycle):
            origin (RecipeRevisionResponseOrigin):
            recipe_id (str):
            revision_number (int):
            schema_version (Literal[1]):
            slug (str):
            title (str):
            content_sha256 (Union[None, Unset, str]):
     """

    created_at: str
    created_by: str
    description: str
    document: 'RecipeRevisionResponseDocument'
    id: str
    lifecycle: RecipeRevisionResponseLifecycle
    origin: RecipeRevisionResponseOrigin
    recipe_id: str
    revision_number: int
    schema_version: Literal[1]
    slug: str
    title: str
    content_sha256: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_revision_response_document import RecipeRevisionResponseDocument
        created_at = self.created_at

        created_by = self.created_by

        description = self.description

        document = self.document.to_dict()

        id = self.id

        lifecycle: str = self.lifecycle

        origin: str = self.origin

        recipe_id = self.recipe_id

        revision_number = self.revision_number

        schema_version = self.schema_version

        slug = self.slug

        title = self.title

        content_sha256: Union[None, Unset, str]
        if isinstance(self.content_sha256, Unset):
            content_sha256 = UNSET
        else:
            content_sha256 = self.content_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "created_at": created_at,
            "created_by": created_by,
            "description": description,
            "document": document,
            "id": id,
            "lifecycle": lifecycle,
            "origin": origin,
            "recipe_id": recipe_id,
            "revision_number": revision_number,
            "schema_version": schema_version,
            "slug": slug,
            "title": title,
        })
        if content_sha256 is not UNSET:
            field_dict["content_sha256"] = content_sha256

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_revision_response_document import RecipeRevisionResponseDocument
        d = dict(src_dict)
        created_at = d.pop("created_at")

        created_by = d.pop("created_by")

        description = d.pop("description")

        document = RecipeRevisionResponseDocument.from_dict(d.pop("document"))




        id = d.pop("id")

        lifecycle = check_recipe_revision_response_lifecycle(d.pop("lifecycle"))




        origin = check_recipe_revision_response_origin(d.pop("origin"))




        recipe_id = d.pop("recipe_id")

        revision_number = d.pop("revision_number")

        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        slug = d.pop("slug")

        title = d.pop("title")

        def _parse_content_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        content_sha256 = _parse_content_sha256(d.pop("content_sha256", UNSET))


        recipe_revision_response = cls(
            created_at=created_at,
            created_by=created_by,
            description=description,
            document=document,
            id=id,
            lifecycle=lifecycle,
            origin=origin,
            recipe_id=recipe_id,
            revision_number=revision_number,
            schema_version=schema_version,
            slug=slug,
            title=title,
            content_sha256=content_sha256,
        )

        return recipe_revision_response
