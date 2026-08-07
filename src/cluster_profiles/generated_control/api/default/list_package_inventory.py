from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.package_inventory_response import PackageInventoryResponse
from ...types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union



def _get_kwargs(
    *,
    node_id: Union[None, Unset, str] = UNSET,
    deployment_id: Union[None, Unset, str] = UNSET,
    cursor: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 20,

) -> dict[str, Any]:




    params: dict[str, Any] = {}

    json_node_id: Union[None, Unset, str]
    if isinstance(node_id, Unset):
        json_node_id = UNSET
    else:
        json_node_id = node_id
    params["node_id"] = json_node_id

    json_deployment_id: Union[None, Unset, str]
    if isinstance(deployment_id, Unset):
        json_deployment_id = UNSET
    else:
        json_deployment_id = deployment_id
    params["deployment_id"] = json_deployment_id

    json_cursor: Union[None, Unset, str]
    if isinstance(cursor, Unset):
        json_cursor = UNSET
    else:
        json_cursor = cursor
    params["cursor"] = json_cursor

    params["limit"] = limit


    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}


    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/packages/inventory",
        "params": params,
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[BoundedErrorResponse, PackageInventoryResponse]]:
    if response.status_code == 200:
        response_200 = PackageInventoryResponse.from_dict(response.json())



        return response_200

    if response.status_code == 401:
        response_401 = BoundedErrorResponse.from_dict(response.json())



        return response_401

    if response.status_code == 422:
        response_422 = BoundedErrorResponse.from_dict(response.json())



        return response_422

    if response.status_code == 503:
        response_503 = BoundedErrorResponse.from_dict(response.json())



        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[BoundedErrorResponse, PackageInventoryResponse]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    node_id: Union[None, Unset, str] = UNSET,
    deployment_id: Union[None, Unset, str] = UNSET,
    cursor: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 20,

) -> Response[Union[BoundedErrorResponse, PackageInventoryResponse]]:
    """ List Inventory

    Args:
        node_id (Union[None, Unset, str]):
        deployment_id (Union[None, Unset, str]):
        cursor (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 20.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, PackageInventoryResponse]]
     """


    kwargs = _get_kwargs(
        node_id=node_id,
deployment_id=deployment_id,
cursor=cursor,
limit=limit,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    *,
    client: AuthenticatedClient,
    node_id: Union[None, Unset, str] = UNSET,
    deployment_id: Union[None, Unset, str] = UNSET,
    cursor: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 20,

) -> Optional[Union[BoundedErrorResponse, PackageInventoryResponse]]:
    """ List Inventory

    Args:
        node_id (Union[None, Unset, str]):
        deployment_id (Union[None, Unset, str]):
        cursor (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 20.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, PackageInventoryResponse]
     """


    return sync_detailed(
        client=client,
node_id=node_id,
deployment_id=deployment_id,
cursor=cursor,
limit=limit,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    node_id: Union[None, Unset, str] = UNSET,
    deployment_id: Union[None, Unset, str] = UNSET,
    cursor: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 20,

) -> Response[Union[BoundedErrorResponse, PackageInventoryResponse]]:
    """ List Inventory

    Args:
        node_id (Union[None, Unset, str]):
        deployment_id (Union[None, Unset, str]):
        cursor (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 20.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, PackageInventoryResponse]]
     """


    kwargs = _get_kwargs(
        node_id=node_id,
deployment_id=deployment_id,
cursor=cursor,
limit=limit,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    *,
    client: AuthenticatedClient,
    node_id: Union[None, Unset, str] = UNSET,
    deployment_id: Union[None, Unset, str] = UNSET,
    cursor: Union[None, Unset, str] = UNSET,
    limit: Union[Unset, int] = 20,

) -> Optional[Union[BoundedErrorResponse, PackageInventoryResponse]]:
    """ List Inventory

    Args:
        node_id (Union[None, Unset, str]):
        deployment_id (Union[None, Unset, str]):
        cursor (Union[None, Unset, str]):
        limit (Union[Unset, int]):  Default: 20.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, PackageInventoryResponse]
     """


    return (await asyncio_detailed(
        client=client,
node_id=node_id,
deployment_id=deployment_id,
cursor=cursor,
limit=limit,

    )).parsed
