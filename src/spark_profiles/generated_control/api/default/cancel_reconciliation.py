from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.cancel_reconciliation_response_cancel_reconciliation_api_v1_reconciliations_reconciliation_id_cancel_post import CancelReconciliationResponseCancelReconciliationApiV1ReconciliationsReconciliationIdCancelPost
from ...models.http_validation_error import HTTPValidationError
from ...models.reconciliation_cancel_request import ReconciliationCancelRequest
from typing import cast



def _get_kwargs(
    reconciliation_id: str,
    *,
    body: ReconciliationCancelRequest,

) -> dict[str, Any]:
    headers: dict[str, Any] = {}






    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/v1/reconciliations/{reconciliation_id}/cancel".format(reconciliation_id=reconciliation_id,),
    }

    _kwargs["json"] = body.to_dict()


    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[CancelReconciliationResponseCancelReconciliationApiV1ReconciliationsReconciliationIdCancelPost, HTTPValidationError]]:
    if response.status_code == 202:
        response_202 = CancelReconciliationResponseCancelReconciliationApiV1ReconciliationsReconciliationIdCancelPost.from_dict(response.json())



        return response_202

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())



        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[CancelReconciliationResponseCancelReconciliationApiV1ReconciliationsReconciliationIdCancelPost, HTTPValidationError]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    reconciliation_id: str,
    *,
    client: AuthenticatedClient,
    body: ReconciliationCancelRequest,

) -> Response[Union[CancelReconciliationResponseCancelReconciliationApiV1ReconciliationsReconciliationIdCancelPost, HTTPValidationError]]:
    """ Cancel Reconciliation

    Args:
        reconciliation_id (str):
        body (ReconciliationCancelRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[CancelReconciliationResponseCancelReconciliationApiV1ReconciliationsReconciliationIdCancelPost, HTTPValidationError]]
     """


    kwargs = _get_kwargs(
        reconciliation_id=reconciliation_id,
body=body,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    reconciliation_id: str,
    *,
    client: AuthenticatedClient,
    body: ReconciliationCancelRequest,

) -> Optional[Union[CancelReconciliationResponseCancelReconciliationApiV1ReconciliationsReconciliationIdCancelPost, HTTPValidationError]]:
    """ Cancel Reconciliation

    Args:
        reconciliation_id (str):
        body (ReconciliationCancelRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[CancelReconciliationResponseCancelReconciliationApiV1ReconciliationsReconciliationIdCancelPost, HTTPValidationError]
     """


    return sync_detailed(
        reconciliation_id=reconciliation_id,
client=client,
body=body,

    ).parsed

async def asyncio_detailed(
    reconciliation_id: str,
    *,
    client: AuthenticatedClient,
    body: ReconciliationCancelRequest,

) -> Response[Union[CancelReconciliationResponseCancelReconciliationApiV1ReconciliationsReconciliationIdCancelPost, HTTPValidationError]]:
    """ Cancel Reconciliation

    Args:
        reconciliation_id (str):
        body (ReconciliationCancelRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[CancelReconciliationResponseCancelReconciliationApiV1ReconciliationsReconciliationIdCancelPost, HTTPValidationError]]
     """


    kwargs = _get_kwargs(
        reconciliation_id=reconciliation_id,
body=body,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    reconciliation_id: str,
    *,
    client: AuthenticatedClient,
    body: ReconciliationCancelRequest,

) -> Optional[Union[CancelReconciliationResponseCancelReconciliationApiV1ReconciliationsReconciliationIdCancelPost, HTTPValidationError]]:
    """ Cancel Reconciliation

    Args:
        reconciliation_id (str):
        body (ReconciliationCancelRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[CancelReconciliationResponseCancelReconciliationApiV1ReconciliationsReconciliationIdCancelPost, HTTPValidationError]
     """


    return (await asyncio_detailed(
        reconciliation_id=reconciliation_id,
client=client,
body=body,

    )).parsed
