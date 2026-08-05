from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.get_repository_response_repository_view_api_v1_repository_get import GetRepositoryResponseRepositoryViewApiV1RepositoryGet
from ...models.http_validation_error import HTTPValidationError
from ...types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union



def _get_kwargs(
    *,
    commit: Union[None, Unset, str] = UNSET,

) -> dict[str, Any]:




    params: dict[str, Any] = {}

    json_commit: Union[None, Unset, str]
    if isinstance(commit, Unset):
        json_commit = UNSET
    else:
        json_commit = commit
    params["commit"] = json_commit


    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}


    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/repository",
        "params": params,
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[GetRepositoryResponseRepositoryViewApiV1RepositoryGet, HTTPValidationError]]:
    if response.status_code == 200:
        response_200 = GetRepositoryResponseRepositoryViewApiV1RepositoryGet.from_dict(response.json())



        return response_200

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())



        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[GetRepositoryResponseRepositoryViewApiV1RepositoryGet, HTTPValidationError]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    commit: Union[None, Unset, str] = UNSET,

) -> Response[Union[GetRepositoryResponseRepositoryViewApiV1RepositoryGet, HTTPValidationError]]:
    """ Repository View

    Args:
        commit (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[GetRepositoryResponseRepositoryViewApiV1RepositoryGet, HTTPValidationError]]
     """


    kwargs = _get_kwargs(
        commit=commit,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    *,
    client: AuthenticatedClient,
    commit: Union[None, Unset, str] = UNSET,

) -> Optional[Union[GetRepositoryResponseRepositoryViewApiV1RepositoryGet, HTTPValidationError]]:
    """ Repository View

    Args:
        commit (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[GetRepositoryResponseRepositoryViewApiV1RepositoryGet, HTTPValidationError]
     """


    return sync_detailed(
        client=client,
commit=commit,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    commit: Union[None, Unset, str] = UNSET,

) -> Response[Union[GetRepositoryResponseRepositoryViewApiV1RepositoryGet, HTTPValidationError]]:
    """ Repository View

    Args:
        commit (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[GetRepositoryResponseRepositoryViewApiV1RepositoryGet, HTTPValidationError]]
     """


    kwargs = _get_kwargs(
        commit=commit,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    *,
    client: AuthenticatedClient,
    commit: Union[None, Unset, str] = UNSET,

) -> Optional[Union[GetRepositoryResponseRepositoryViewApiV1RepositoryGet, HTTPValidationError]]:
    """ Repository View

    Args:
        commit (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[GetRepositoryResponseRepositoryViewApiV1RepositoryGet, HTTPValidationError]
     """


    return (await asyncio_detailed(
        client=client,
commit=commit,

    )).parsed
