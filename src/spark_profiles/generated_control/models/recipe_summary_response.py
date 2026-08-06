from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_summary_response_lifecycle import check_recipe_summary_response_lifecycle
from ..models.recipe_summary_response_lifecycle import RecipeSummaryResponseLifecycle
from ..models.recipe_summary_response_origin import check_recipe_summary_response_origin
from ..models.recipe_summary_response_origin import RecipeSummaryResponseOrigin
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="RecipeSummaryResponse")



@_attrs_define
class RecipeSummaryResponse:
    """
        Attributes:
            lifecycle (RecipeSummaryResponseLifecycle):
            origin (RecipeSummaryResponseOrigin):
            recipe_id (str):
            revision_number (int):
            slug (str):
            title (str):
            content_sha256 (Union[None, Unset, str]):
     """

    lifecycle: RecipeSummaryResponseLifecycle
    origin: RecipeSummaryResponseOrigin
    recipe_id: str
    revision_number: int
    slug: str
    title: str
    content_sha256: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        lifecycle: str = self.lifecycle

        origin: str = self.origin

        recipe_id = self.recipe_id

        revision_number = self.revision_number

        slug = self.slug

        title = self.title

        content_sha256: Union[None, Unset, str]
        if isinstance(self.content_sha256, Unset):
            content_sha256 = UNSET
        else:
            content_sha256 = self.content_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "lifecycle": lifecycle,
            "origin": origin,
            "recipe_id": recipe_id,
            "revision_number": revision_number,
            "slug": slug,
            "title": title,
        })
        if content_sha256 is not UNSET:
            field_dict["content_sha256"] = content_sha256

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        lifecycle = check_recipe_summary_response_lifecycle(d.pop("lifecycle"))




        origin = check_recipe_summary_response_origin(d.pop("origin"))




        recipe_id = d.pop("recipe_id")

        revision_number = d.pop("revision_number")

        slug = d.pop("slug")

        title = d.pop("title")

        def _parse_content_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        content_sha256 = _parse_content_sha256(d.pop("content_sha256", UNSET))


        recipe_summary_response = cls(
            lifecycle=lifecycle,
            origin=origin,
            recipe_id=recipe_id,
            revision_number=revision_number,
            slug=slug,
            title=title,
            content_sha256=content_sha256,
        )

        return recipe_summary_response
