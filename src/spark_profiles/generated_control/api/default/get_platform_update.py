from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.get_platform_update_response_update_status_api_v1_updates_rollout_id_get import GetPlatformUpdateResponseUpdateStatusApiV1UpdatesRolloutIdGet
from ...models.http_validation_error import HTTPValidationError
from typing import cast



def _get_kwargs(
    rollout_id: str,

) -> dict[str, Any]:






    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/updates/{rollout_id}".format(rollout_id=rollout_id,),
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[GetPlatformUpdateResponseUpdateStatusApiV1UpdatesRolloutIdGet, HTTPValidationError]]:
    if response.status_code == 200:
        response_200 = GetPlatformUpdateResponseUpdateStatusApiV1UpdatesRolloutIdGet.from_dict(response.json())



        return response_200

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())



        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[GetPlatformUpdateResponseUpdateStatusApiV1UpdatesRolloutIdGet, HTTPValidationError]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    rollout_id: str,
    *,
    client: AuthenticatedClient,

) -> Response[Union[GetPlatformUpdateResponseUpdateStatusApiV1UpdatesRolloutIdGet, HTTPValidationError]]:
    """ Update Status

    Args:
        rollout_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[GetPlatformUpdateResponseUpdateStatusApiV1UpdatesRolloutIdGet, HTTPValidationError]]
     """


    kwargs = _get_kwargs(
        rollout_id=rollout_id,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    rollout_id: str,
    *,
    client: AuthenticatedClient,

) -> Optional[Union[GetPlatformUpdateResponseUpdateStatusApiV1UpdatesRolloutIdGet, HTTPValidationError]]:
    """ Update Status

    Args:
        rollout_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[GetPlatformUpdateResponseUpdateStatusApiV1UpdatesRolloutIdGet, HTTPValidationError]
     """


    return sync_detailed(
        rollout_id=rollout_id,
client=client,

    ).parsed

async def asyncio_detailed(
    rollout_id: str,
    *,
    client: AuthenticatedClient,

) -> Response[Union[GetPlatformUpdateResponseUpdateStatusApiV1UpdatesRolloutIdGet, HTTPValidationError]]:
    """ Update Status

    Args:
        rollout_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[GetPlatformUpdateResponseUpdateStatusApiV1UpdatesRolloutIdGet, HTTPValidationError]]
     """


    kwargs = _get_kwargs(
        rollout_id=rollout_id,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    rollout_id: str,
    *,
    client: AuthenticatedClient,

) -> Optional[Union[GetPlatformUpdateResponseUpdateStatusApiV1UpdatesRolloutIdGet, HTTPValidationError]]:
    """ Update Status

    Args:
        rollout_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[GetPlatformUpdateResponseUpdateStatusApiV1UpdatesRolloutIdGet, HTTPValidationError]
     """


    return (await asyncio_detailed(
        rollout_id=rollout_id,
client=client,

    )).parsed
