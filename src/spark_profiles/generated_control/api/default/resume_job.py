from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.http_validation_error import HTTPValidationError
from ...models.job_resume_response import JobResumeResponse
from typing import cast



def _get_kwargs(
    job_id: str,
    *,
    body: None,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}






    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/jobs/{job_id}/resume".format(job_id=job_id,),
    }

    _kwargs["json"] = body


    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[HTTPValidationError, JobResumeResponse]]:
    if response.status_code == 202:
        response_202 = JobResumeResponse.from_dict(response.json())



        return response_202

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())



        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[HTTPValidationError, JobResumeResponse]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    job_id: str,
    *,
    client: AuthenticatedClient,
    body: None,

) -> Response[Union[HTTPValidationError, JobResumeResponse]]:
    """ Resume Job

    Args:
        job_id (str):
        body (None):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[HTTPValidationError, JobResumeResponse]]
     """


    kwargs = _get_kwargs(
        job_id=job_id,
body=body,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    job_id: str,
    *,
    client: AuthenticatedClient,
    body: None,

) -> Optional[Union[HTTPValidationError, JobResumeResponse]]:
    """ Resume Job

    Args:
        job_id (str):
        body (None):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[HTTPValidationError, JobResumeResponse]
     """


    return sync_detailed(
        job_id=job_id,
client=client,
body=body,

    ).parsed

async def asyncio_detailed(
    job_id: str,
    *,
    client: AuthenticatedClient,
    body: None,

) -> Response[Union[HTTPValidationError, JobResumeResponse]]:
    """ Resume Job

    Args:
        job_id (str):
        body (None):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[HTTPValidationError, JobResumeResponse]]
     """


    kwargs = _get_kwargs(
        job_id=job_id,
body=body,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    job_id: str,
    *,
    client: AuthenticatedClient,
    body: None,

) -> Optional[Union[HTTPValidationError, JobResumeResponse]]:
    """ Resume Job

    Args:
        job_id (str):
        body (None):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[HTTPValidationError, JobResumeResponse]
     """


    return (await asyncio_detailed(
        job_id=job_id,
client=client,
body=body,

    )).parsed
