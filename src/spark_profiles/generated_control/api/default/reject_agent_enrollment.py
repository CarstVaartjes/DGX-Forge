from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.http_validation_error import HTTPValidationError
from ...models.reject_agent_enrollment_response_reject_api_v1_agents_enrollments_enrollment_id_reject_post import RejectAgentEnrollmentResponseRejectApiV1AgentsEnrollmentsEnrollmentIdRejectPost
from ...models.reject_request import RejectRequest
from typing import cast



def _get_kwargs(
    enrollment_id: str,
    *,
    body: RejectRequest,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}






    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/agents/enrollments/{enrollment_id}/reject".format(enrollment_id=enrollment_id,),
    }

    _kwargs["json"] = body.to_dict()


    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[HTTPValidationError, RejectAgentEnrollmentResponseRejectApiV1AgentsEnrollmentsEnrollmentIdRejectPost]]:
    if response.status_code == 200:
        response_200 = RejectAgentEnrollmentResponseRejectApiV1AgentsEnrollmentsEnrollmentIdRejectPost.from_dict(response.json())



        return response_200

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())



        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[HTTPValidationError, RejectAgentEnrollmentResponseRejectApiV1AgentsEnrollmentsEnrollmentIdRejectPost]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    enrollment_id: str,
    *,
    client: AuthenticatedClient,
    body: RejectRequest,

) -> Response[Union[HTTPValidationError, RejectAgentEnrollmentResponseRejectApiV1AgentsEnrollmentsEnrollmentIdRejectPost]]:
    """ Reject

    Args:
        enrollment_id (str):
        body (RejectRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[HTTPValidationError, RejectAgentEnrollmentResponseRejectApiV1AgentsEnrollmentsEnrollmentIdRejectPost]]
     """


    kwargs = _get_kwargs(
        enrollment_id=enrollment_id,
body=body,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    enrollment_id: str,
    *,
    client: AuthenticatedClient,
    body: RejectRequest,

) -> Optional[Union[HTTPValidationError, RejectAgentEnrollmentResponseRejectApiV1AgentsEnrollmentsEnrollmentIdRejectPost]]:
    """ Reject

    Args:
        enrollment_id (str):
        body (RejectRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[HTTPValidationError, RejectAgentEnrollmentResponseRejectApiV1AgentsEnrollmentsEnrollmentIdRejectPost]
     """


    return sync_detailed(
        enrollment_id=enrollment_id,
client=client,
body=body,

    ).parsed

async def asyncio_detailed(
    enrollment_id: str,
    *,
    client: AuthenticatedClient,
    body: RejectRequest,

) -> Response[Union[HTTPValidationError, RejectAgentEnrollmentResponseRejectApiV1AgentsEnrollmentsEnrollmentIdRejectPost]]:
    """ Reject

    Args:
        enrollment_id (str):
        body (RejectRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[HTTPValidationError, RejectAgentEnrollmentResponseRejectApiV1AgentsEnrollmentsEnrollmentIdRejectPost]]
     """


    kwargs = _get_kwargs(
        enrollment_id=enrollment_id,
body=body,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    enrollment_id: str,
    *,
    client: AuthenticatedClient,
    body: RejectRequest,

) -> Optional[Union[HTTPValidationError, RejectAgentEnrollmentResponseRejectApiV1AgentsEnrollmentsEnrollmentIdRejectPost]]:
    """ Reject

    Args:
        enrollment_id (str):
        body (RejectRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[HTTPValidationError, RejectAgentEnrollmentResponseRejectApiV1AgentsEnrollmentsEnrollmentIdRejectPost]
     """


    return (await asyncio_detailed(
        enrollment_id=enrollment_id,
client=client,
body=body,

    )).parsed
