from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.catalog_problem import CatalogProblem
from ...models.http_validation_error import HTTPValidationError
from ...models.recipe_revision_response import RecipeRevisionResponse
from typing import cast



def _get_kwargs(
    recipe_id: str,

) -> dict[str, Any]:






    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/catalog/recipes/{recipe_id}".format(recipe_id=recipe_id,),
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[CatalogProblem, HTTPValidationError, RecipeRevisionResponse]]:
    if response.status_code == 200:
        response_200 = RecipeRevisionResponse.from_dict(response.json())



        return response_200

    if response.status_code == 401:
        response_401 = CatalogProblem.from_dict(response.json())



        return response_401

    if response.status_code == 404:
        response_404 = CatalogProblem.from_dict(response.json())



        return response_404

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())



        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[CatalogProblem, HTTPValidationError, RecipeRevisionResponse]]:
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

) -> Response[Union[CatalogProblem, HTTPValidationError, RecipeRevisionResponse]]:
    """ Get Recipe

    Args:
        recipe_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[CatalogProblem, HTTPValidationError, RecipeRevisionResponse]]
     """


    kwargs = _get_kwargs(
        recipe_id=recipe_id,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    recipe_id: str,
    *,
    client: AuthenticatedClient,

) -> Optional[Union[CatalogProblem, HTTPValidationError, RecipeRevisionResponse]]:
    """ Get Recipe

    Args:
        recipe_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[CatalogProblem, HTTPValidationError, RecipeRevisionResponse]
     """


    return sync_detailed(
        recipe_id=recipe_id,
client=client,

    ).parsed

async def asyncio_detailed(
    recipe_id: str,
    *,
    client: AuthenticatedClient,

) -> Response[Union[CatalogProblem, HTTPValidationError, RecipeRevisionResponse]]:
    """ Get Recipe

    Args:
        recipe_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[CatalogProblem, HTTPValidationError, RecipeRevisionResponse]]
     """


    kwargs = _get_kwargs(
        recipe_id=recipe_id,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    recipe_id: str,
    *,
    client: AuthenticatedClient,

) -> Optional[Union[CatalogProblem, HTTPValidationError, RecipeRevisionResponse]]:
    """ Get Recipe

    Args:
        recipe_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[CatalogProblem, HTTPValidationError, RecipeRevisionResponse]
     """


    return (await asyncio_detailed(
        recipe_id=recipe_id,
client=client,

    )).parsed
