from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.catalog_problem import CatalogProblem
from ...models.fork_recipe_request import ForkRecipeRequest
from ...models.recipe_revision_response import RecipeRevisionResponse
from typing import cast



def _get_kwargs(
    recipe_id: str,
    *,
    body: ForkRecipeRequest,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}






    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/catalog/recipes/{recipe_id}/fork".format(recipe_id=recipe_id,),
    }

    _kwargs["json"] = body.to_dict()


    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[CatalogProblem, RecipeRevisionResponse]]:
    if response.status_code == 201:
        response_201 = RecipeRevisionResponse.from_dict(response.json())



        return response_201

    if response.status_code == 401:
        response_401 = CatalogProblem.from_dict(response.json())



        return response_401

    if response.status_code == 403:
        response_403 = CatalogProblem.from_dict(response.json())



        return response_403

    if response.status_code == 404:
        response_404 = CatalogProblem.from_dict(response.json())



        return response_404

    if response.status_code == 409:
        response_409 = CatalogProblem.from_dict(response.json())



        return response_409

    if response.status_code == 422:
        response_422 = CatalogProblem.from_dict(response.json())



        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[CatalogProblem, RecipeRevisionResponse]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    recipe_id: str,
    *,
    client: AuthenticatedClient,
    body: ForkRecipeRequest,

) -> Response[Union[CatalogProblem, RecipeRevisionResponse]]:
    """ Fork Recipe

    Args:
        recipe_id (str):
        body (ForkRecipeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[CatalogProblem, RecipeRevisionResponse]]
     """


    kwargs = _get_kwargs(
        recipe_id=recipe_id,
body=body,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    recipe_id: str,
    *,
    client: AuthenticatedClient,
    body: ForkRecipeRequest,

) -> Optional[Union[CatalogProblem, RecipeRevisionResponse]]:
    """ Fork Recipe

    Args:
        recipe_id (str):
        body (ForkRecipeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[CatalogProblem, RecipeRevisionResponse]
     """


    return sync_detailed(
        recipe_id=recipe_id,
client=client,
body=body,

    ).parsed

async def asyncio_detailed(
    recipe_id: str,
    *,
    client: AuthenticatedClient,
    body: ForkRecipeRequest,

) -> Response[Union[CatalogProblem, RecipeRevisionResponse]]:
    """ Fork Recipe

    Args:
        recipe_id (str):
        body (ForkRecipeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[CatalogProblem, RecipeRevisionResponse]]
     """


    kwargs = _get_kwargs(
        recipe_id=recipe_id,
body=body,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    recipe_id: str,
    *,
    client: AuthenticatedClient,
    body: ForkRecipeRequest,

) -> Optional[Union[CatalogProblem, RecipeRevisionResponse]]:
    """ Fork Recipe

    Args:
        recipe_id (str):
        body (ForkRecipeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[CatalogProblem, RecipeRevisionResponse]
     """


    return (await asyncio_detailed(
        recipe_id=recipe_id,
client=client,
body=body,

    )).parsed
