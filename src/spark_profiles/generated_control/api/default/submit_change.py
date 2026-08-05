from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.change_request import ChangeRequest
from ...models.http_validation_error import HTTPValidationError
from ...models.submit_change_response_submit_change_api_v1_changes_post import SubmitChangeResponseSubmitChangeApiV1ChangesPost
from typing import cast



def _get_kwargs(
    *,
    body: ChangeRequest,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}






    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/changes",
    }

    _kwargs["json"] = body.to_dict()


    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[HTTPValidationError, SubmitChangeResponseSubmitChangeApiV1ChangesPost]]:
    if response.status_code == 202:
        response_202 = SubmitChangeResponseSubmitChangeApiV1ChangesPost.from_dict(response.json())



        return response_202

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())



        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[HTTPValidationError, SubmitChangeResponseSubmitChangeApiV1ChangesPost]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    body: ChangeRequest,

) -> Response[Union[HTTPValidationError, SubmitChangeResponseSubmitChangeApiV1ChangesPost]]:
    """ Submit Change

    Args:
        body (ChangeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[HTTPValidationError, SubmitChangeResponseSubmitChangeApiV1ChangesPost]]
     """


    kwargs = _get_kwargs(
        body=body,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    *,
    client: AuthenticatedClient,
    body: ChangeRequest,

) -> Optional[Union[HTTPValidationError, SubmitChangeResponseSubmitChangeApiV1ChangesPost]]:
    """ Submit Change

    Args:
        body (ChangeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[HTTPValidationError, SubmitChangeResponseSubmitChangeApiV1ChangesPost]
     """


    return sync_detailed(
        client=client,
body=body,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: ChangeRequest,

) -> Response[Union[HTTPValidationError, SubmitChangeResponseSubmitChangeApiV1ChangesPost]]:
    """ Submit Change

    Args:
        body (ChangeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[HTTPValidationError, SubmitChangeResponseSubmitChangeApiV1ChangesPost]]
     """


    kwargs = _get_kwargs(
        body=body,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    *,
    client: AuthenticatedClient,
    body: ChangeRequest,

) -> Optional[Union[HTTPValidationError, SubmitChangeResponseSubmitChangeApiV1ChangesPost]]:
    """ Submit Change

    Args:
        body (ChangeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[HTTPValidationError, SubmitChangeResponseSubmitChangeApiV1ChangesPost]
     """


    return (await asyncio_detailed(
        client=client,
body=body,

    )).parsed
