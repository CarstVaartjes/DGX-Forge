from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.package_progress_response import PackageProgressResponse
from typing import cast



def _get_kwargs(
    validation_id: str,

) -> dict[str, Any]:






    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/packages/validations/{validation_id}".format(validation_id=validation_id,),
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[BoundedErrorResponse, PackageProgressResponse]]:
    if response.status_code == 200:
        response_200 = PackageProgressResponse.from_dict(response.json())



        return response_200

    if response.status_code == 401:
        response_401 = BoundedErrorResponse.from_dict(response.json())



        return response_401

    if response.status_code == 404:
        response_404 = BoundedErrorResponse.from_dict(response.json())



        return response_404

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


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[BoundedErrorResponse, PackageProgressResponse]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    validation_id: str,
    *,
    client: AuthenticatedClient,

) -> Response[Union[BoundedErrorResponse, PackageProgressResponse]]:
    """ Get Validation

    Args:
        validation_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, PackageProgressResponse]]
     """


    kwargs = _get_kwargs(
        validation_id=validation_id,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    validation_id: str,
    *,
    client: AuthenticatedClient,

) -> Optional[Union[BoundedErrorResponse, PackageProgressResponse]]:
    """ Get Validation

    Args:
        validation_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, PackageProgressResponse]
     """


    return sync_detailed(
        validation_id=validation_id,
client=client,

    ).parsed

async def asyncio_detailed(
    validation_id: str,
    *,
    client: AuthenticatedClient,

) -> Response[Union[BoundedErrorResponse, PackageProgressResponse]]:
    """ Get Validation

    Args:
        validation_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, PackageProgressResponse]]
     """


    kwargs = _get_kwargs(
        validation_id=validation_id,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    validation_id: str,
    *,
    client: AuthenticatedClient,

) -> Optional[Union[BoundedErrorResponse, PackageProgressResponse]]:
    """ Get Validation

    Args:
        validation_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, PackageProgressResponse]
     """


    return (await asyncio_detailed(
        validation_id=validation_id,
client=client,

    )).parsed
