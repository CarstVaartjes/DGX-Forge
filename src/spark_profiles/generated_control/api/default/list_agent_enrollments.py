from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.http_validation_error import HTTPValidationError
from ...models.list_agent_enrollments_response_list_enrollments_api_v1_agents_enrollments_get import ListAgentEnrollmentsResponseListEnrollmentsApiV1AgentsEnrollmentsGet
from ...types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union



def _get_kwargs(
    *,
    cursor: Union[None, Unset, str] = UNSET,
    state: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 100,

) -> dict[str, Any]:




    params: dict[str, Any] = {}

    json_cursor: Union[None, Unset, str]
    if isinstance(cursor, Unset):
        json_cursor = UNSET
    else:
        json_cursor = cursor
    params["cursor"] = json_cursor

    json_state: Union[None, Unset, str]
    if isinstance(state, Unset):
        json_state = UNSET
    else:
        json_state = state
    params["state"] = json_state

    params["limit"] = limit


    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}


    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/agents/enrollments",
        "params": params,
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[HTTPValidationError, ListAgentEnrollmentsResponseListEnrollmentsApiV1AgentsEnrollmentsGet]]:
    if response.status_code == 200:
        response_200 = ListAgentEnrollmentsResponseListEnrollmentsApiV1AgentsEnrollmentsGet.from_dict(response.json())



        return response_200

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())



        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[HTTPValidationError, ListAgentEnrollmentsResponseListEnrollmentsApiV1AgentsEnrollmentsGet]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    cursor: Union[None, Unset, str] = UNSET,
    state: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 100,

) -> Response[Union[HTTPValidationError, ListAgentEnrollmentsResponseListEnrollmentsApiV1AgentsEnrollmentsGet]]:
    """ List Enrollments

    Args:
        cursor (Union[None, Unset, str]):
        state (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 100.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[HTTPValidationError, ListAgentEnrollmentsResponseListEnrollmentsApiV1AgentsEnrollmentsGet]]
     """


    kwargs = _get_kwargs(
        cursor=cursor,
state=state,
limit=limit,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    *,
    client: AuthenticatedClient,
    cursor: Union[None, Unset, str] = UNSET,
    state: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 100,

) -> Optional[Union[HTTPValidationError, ListAgentEnrollmentsResponseListEnrollmentsApiV1AgentsEnrollmentsGet]]:
    """ List Enrollments

    Args:
        cursor (Union[None, Unset, str]):
        state (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 100.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[HTTPValidationError, ListAgentEnrollmentsResponseListEnrollmentsApiV1AgentsEnrollmentsGet]
     """


    return sync_detailed(
        client=client,
cursor=cursor,
state=state,
limit=limit,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    cursor: Union[None, Unset, str] = UNSET,
    state: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 100,

) -> Response[Union[HTTPValidationError, ListAgentEnrollmentsResponseListEnrollmentsApiV1AgentsEnrollmentsGet]]:
    """ List Enrollments

    Args:
        cursor (Union[None, Unset, str]):
        state (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 100.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[HTTPValidationError, ListAgentEnrollmentsResponseListEnrollmentsApiV1AgentsEnrollmentsGet]]
     """


    kwargs = _get_kwargs(
        cursor=cursor,
state=state,
limit=limit,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    *,
    client: AuthenticatedClient,
    cursor: Union[None, Unset, str] = UNSET,
    state: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 100,

) -> Optional[Union[HTTPValidationError, ListAgentEnrollmentsResponseListEnrollmentsApiV1AgentsEnrollmentsGet]]:
    """ List Enrollments

    Args:
        cursor (Union[None, Unset, str]):
        state (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 100.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[HTTPValidationError, ListAgentEnrollmentsResponseListEnrollmentsApiV1AgentsEnrollmentsGet]
     """


    return (await asyncio_detailed(
        client=client,
cursor=cursor,
state=state,
limit=limit,

    )).parsed
