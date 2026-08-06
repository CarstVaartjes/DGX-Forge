from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.bounded_error_response import BoundedErrorResponse
from ...models.package_promotion_request import PackagePromotionRequest
from ...models.package_promotion_response import PackagePromotionResponse
from typing import cast



def _get_kwargs(
    candidate_id: str,
    *,
    body: PackagePromotionRequest,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}






    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/packages/candidates/{candidate_id}/promote".format(candidate_id=candidate_id,),
    }

    _kwargs["json"] = body.to_dict()


    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[BoundedErrorResponse, PackagePromotionResponse]]:
    if response.status_code == 202:
        response_202 = PackagePromotionResponse.from_dict(response.json())



        return response_202

    if response.status_code == 401:
        response_401 = BoundedErrorResponse.from_dict(response.json())



        return response_401

    if response.status_code == 403:
        response_403 = BoundedErrorResponse.from_dict(response.json())



        return response_403

    if response.status_code == 404:
        response_404 = BoundedErrorResponse.from_dict(response.json())



        return response_404

    if response.status_code == 409:
        response_409 = BoundedErrorResponse.from_dict(response.json())



        return response_409

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


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[BoundedErrorResponse, PackagePromotionResponse]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    candidate_id: str,
    *,
    client: AuthenticatedClient,
    body: PackagePromotionRequest,

) -> Response[Union[BoundedErrorResponse, PackagePromotionResponse]]:
    """ Promote Package

    Args:
        candidate_id (str):
        body (PackagePromotionRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, PackagePromotionResponse]]
     """


    kwargs = _get_kwargs(
        candidate_id=candidate_id,
body=body,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    candidate_id: str,
    *,
    client: AuthenticatedClient,
    body: PackagePromotionRequest,

) -> Optional[Union[BoundedErrorResponse, PackagePromotionResponse]]:
    """ Promote Package

    Args:
        candidate_id (str):
        body (PackagePromotionRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, PackagePromotionResponse]
     """


    return sync_detailed(
        candidate_id=candidate_id,
client=client,
body=body,

    ).parsed

async def asyncio_detailed(
    candidate_id: str,
    *,
    client: AuthenticatedClient,
    body: PackagePromotionRequest,

) -> Response[Union[BoundedErrorResponse, PackagePromotionResponse]]:
    """ Promote Package

    Args:
        candidate_id (str):
        body (PackagePromotionRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[BoundedErrorResponse, PackagePromotionResponse]]
     """


    kwargs = _get_kwargs(
        candidate_id=candidate_id,
body=body,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    candidate_id: str,
    *,
    client: AuthenticatedClient,
    body: PackagePromotionRequest,

) -> Optional[Union[BoundedErrorResponse, PackagePromotionResponse]]:
    """ Promote Package

    Args:
        candidate_id (str):
        body (PackagePromotionRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[BoundedErrorResponse, PackagePromotionResponse]
     """


    return (await asyncio_detailed(
        candidate_id=candidate_id,
client=client,
body=body,

    )).parsed
