from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.approve_platform_update_recovery_response_approve_update_resume_api_v1_updates_rollout_id_approve_resume_post import ApprovePlatformUpdateRecoveryResponseApproveUpdateResumeApiV1UpdatesRolloutIdApproveResumePost
from ...models.http_validation_error import HTTPValidationError
from ...models.update_approve_resume_request import UpdateApproveResumeRequest
from typing import cast



def _get_kwargs(
    rollout_id: str,
    *,
    body: UpdateApproveResumeRequest,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}






    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/updates/{rollout_id}/approve-resume".format(rollout_id=rollout_id,),
    }

    _kwargs["json"] = body.to_dict()


    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[ApprovePlatformUpdateRecoveryResponseApproveUpdateResumeApiV1UpdatesRolloutIdApproveResumePost, HTTPValidationError]]:
    if response.status_code == 202:
        response_202 = ApprovePlatformUpdateRecoveryResponseApproveUpdateResumeApiV1UpdatesRolloutIdApproveResumePost.from_dict(response.json())



        return response_202

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())



        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[ApprovePlatformUpdateRecoveryResponseApproveUpdateResumeApiV1UpdatesRolloutIdApproveResumePost, HTTPValidationError]]:
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
    body: UpdateApproveResumeRequest,

) -> Response[Union[ApprovePlatformUpdateRecoveryResponseApproveUpdateResumeApiV1UpdatesRolloutIdApproveResumePost, HTTPValidationError]]:
    """ Approve Update Resume

    Args:
        rollout_id (str):
        body (UpdateApproveResumeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[ApprovePlatformUpdateRecoveryResponseApproveUpdateResumeApiV1UpdatesRolloutIdApproveResumePost, HTTPValidationError]]
     """


    kwargs = _get_kwargs(
        rollout_id=rollout_id,
body=body,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    rollout_id: str,
    *,
    client: AuthenticatedClient,
    body: UpdateApproveResumeRequest,

) -> Optional[Union[ApprovePlatformUpdateRecoveryResponseApproveUpdateResumeApiV1UpdatesRolloutIdApproveResumePost, HTTPValidationError]]:
    """ Approve Update Resume

    Args:
        rollout_id (str):
        body (UpdateApproveResumeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[ApprovePlatformUpdateRecoveryResponseApproveUpdateResumeApiV1UpdatesRolloutIdApproveResumePost, HTTPValidationError]
     """


    return sync_detailed(
        rollout_id=rollout_id,
client=client,
body=body,

    ).parsed

async def asyncio_detailed(
    rollout_id: str,
    *,
    client: AuthenticatedClient,
    body: UpdateApproveResumeRequest,

) -> Response[Union[ApprovePlatformUpdateRecoveryResponseApproveUpdateResumeApiV1UpdatesRolloutIdApproveResumePost, HTTPValidationError]]:
    """ Approve Update Resume

    Args:
        rollout_id (str):
        body (UpdateApproveResumeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[ApprovePlatformUpdateRecoveryResponseApproveUpdateResumeApiV1UpdatesRolloutIdApproveResumePost, HTTPValidationError]]
     """


    kwargs = _get_kwargs(
        rollout_id=rollout_id,
body=body,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    rollout_id: str,
    *,
    client: AuthenticatedClient,
    body: UpdateApproveResumeRequest,

) -> Optional[Union[ApprovePlatformUpdateRecoveryResponseApproveUpdateResumeApiV1UpdatesRolloutIdApproveResumePost, HTTPValidationError]]:
    """ Approve Update Resume

    Args:
        rollout_id (str):
        body (UpdateApproveResumeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[ApprovePlatformUpdateRecoveryResponseApproveUpdateResumeApiV1UpdatesRolloutIdApproveResumePost, HTTPValidationError]
     """


    return (await asyncio_detailed(
        rollout_id=rollout_id,
client=client,
body=body,

    )).parsed
