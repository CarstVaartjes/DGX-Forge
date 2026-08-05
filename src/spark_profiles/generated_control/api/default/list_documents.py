from http import HTTPStatus
from typing import Any, Optional, Union, cast

import httpx

from ...client import AuthenticatedClient, Client
from ...types import Response, UNSET
from ... import errors

from ...models.http_validation_error import HTTPValidationError
from ...models.list_documents_response_document_view_api_v1_documents_get import ListDocumentsResponseDocumentViewApiV1DocumentsGet
from ...types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union



def _get_kwargs(
    *,
    commit: Union[None, Unset, str] = UNSET,
    path: Union[None, Unset, str] = UNSET,
    kind: Union[None, Unset, str] = UNSET,

) -> dict[str, Any]:




    params: dict[str, Any] = {}

    json_commit: Union[None, Unset, str]
    if isinstance(commit, Unset):
        json_commit = UNSET
    else:
        json_commit = commit
    params["commit"] = json_commit

    json_path: Union[None, Unset, str]
    if isinstance(path, Unset):
        json_path = UNSET
    else:
        json_path = path
    params["path"] = json_path

    json_kind: Union[None, Unset, str]
    if isinstance(kind, Unset):
        json_kind = UNSET
    else:
        json_kind = kind
    params["kind"] = json_kind


    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}


    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/v1/documents",
        "params": params,
    }


    return _kwargs



def _parse_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Optional[Union[HTTPValidationError, ListDocumentsResponseDocumentViewApiV1DocumentsGet]]:
    if response.status_code == 200:
        response_200 = ListDocumentsResponseDocumentViewApiV1DocumentsGet.from_dict(response.json())



        return response_200

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())



        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(*, client: Union[AuthenticatedClient, Client], response: httpx.Response) -> Response[Union[HTTPValidationError, ListDocumentsResponseDocumentViewApiV1DocumentsGet]]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    commit: Union[None, Unset, str] = UNSET,
    path: Union[None, Unset, str] = UNSET,
    kind: Union[None, Unset, str] = UNSET,

) -> Response[Union[HTTPValidationError, ListDocumentsResponseDocumentViewApiV1DocumentsGet]]:
    """ Document View

    Args:
        commit (Union[None, Unset, str]):
        path (Union[None, Unset, str]):
        kind (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[HTTPValidationError, ListDocumentsResponseDocumentViewApiV1DocumentsGet]]
     """


    kwargs = _get_kwargs(
        commit=commit,
path=path,
kind=kind,

    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)

def sync(
    *,
    client: AuthenticatedClient,
    commit: Union[None, Unset, str] = UNSET,
    path: Union[None, Unset, str] = UNSET,
    kind: Union[None, Unset, str] = UNSET,

) -> Optional[Union[HTTPValidationError, ListDocumentsResponseDocumentViewApiV1DocumentsGet]]:
    """ Document View

    Args:
        commit (Union[None, Unset, str]):
        path (Union[None, Unset, str]):
        kind (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[HTTPValidationError, ListDocumentsResponseDocumentViewApiV1DocumentsGet]
     """


    return sync_detailed(
        client=client,
commit=commit,
path=path,
kind=kind,

    ).parsed

async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    commit: Union[None, Unset, str] = UNSET,
    path: Union[None, Unset, str] = UNSET,
    kind: Union[None, Unset, str] = UNSET,

) -> Response[Union[HTTPValidationError, ListDocumentsResponseDocumentViewApiV1DocumentsGet]]:
    """ Document View

    Args:
        commit (Union[None, Unset, str]):
        path (Union[None, Unset, str]):
        kind (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Union[HTTPValidationError, ListDocumentsResponseDocumentViewApiV1DocumentsGet]]
     """


    kwargs = _get_kwargs(
        commit=commit,
path=path,
kind=kind,

    )

    response = await client.get_async_httpx_client().request(
        **kwargs
    )

    return _build_response(client=client, response=response)

async def asyncio(
    *,
    client: AuthenticatedClient,
    commit: Union[None, Unset, str] = UNSET,
    path: Union[None, Unset, str] = UNSET,
    kind: Union[None, Unset, str] = UNSET,

) -> Optional[Union[HTTPValidationError, ListDocumentsResponseDocumentViewApiV1DocumentsGet]]:
    """ Document View

    Args:
        commit (Union[None, Unset, str]):
        path (Union[None, Unset, str]):
        kind (Union[None, Unset, str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Union[HTTPValidationError, ListDocumentsResponseDocumentViewApiV1DocumentsGet]
     """


    return (await asyncio_detailed(
        client=client,
commit=commit,
path=path,
kind=kind,

    )).parsed
