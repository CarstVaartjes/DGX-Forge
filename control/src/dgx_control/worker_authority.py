"""Authenticated internal repository authority for the repository-less worker."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict
from typing import Protocol

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .litellm import LiteLlmDeployment, LiteLlmPublisher
from .route_runtime import PublishedRoute

_COMMIT = re.compile(r"[0-9a-f]{40,64}\Z")
_TOKEN = re.compile(rb"[A-Za-z0-9_-]{32,}\Z")
_MAX_RESPONSE = 65_536
_MAX_ATTESTATION_SECONDS = 15


class WorkerAuthorityError(RuntimeError):
    """The internal authority was unavailable or returned unsafe data."""


class DeploymentPolicy(Protocol):
    def __call__(
        self,
        commit: str,
        routes: tuple[PublishedRoute, ...],
    ) -> tuple[LiteLlmDeployment, ...]: ...


class AuthorityRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1, max_length=128)
    workload_id: str = Field(min_length=1, max_length=128)
    api_base: str = Field(min_length=1, max_length=512)
    requests_per_minute: int = Field(ge=1, le=100_000)
    tokens_per_minute: int = Field(ge=1, le=100_000_000)


class AuthorityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1, le=1)
    commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    nonce: str = Field(pattern=r"^[0-9a-f]{32,64}$")
    routes: list[AuthorityRoute] = Field(max_length=64)


class RepositoryAuthorityService:
    """Evaluate live Git authority and repository policy inside the API."""

    def __init__(
        self,
        *,
        current_commit: Callable[[], str],
        commit_eligible: Callable[[str], bool],
        deployments: DeploymentPolicy,
        clock: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        self._current_commit = current_commit
        self._commit_eligible = commit_eligible
        self._deployments = deployments
        self._clock = clock

    def current(self) -> str:
        commit = self._current_commit()
        if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
            raise WorkerAuthorityError("repository head is invalid")
        return commit

    def evaluate(
        self,
        commit: str,
        routes: tuple[PublishedRoute, ...],
    ) -> Mapping[str, object]:
        if _COMMIT.fullmatch(commit) is None:
            raise WorkerAuthorityError("repository commit is invalid")
        current = secrets.compare_digest(self.current(), commit)
        eligible = current and self._commit_eligible(commit) is True
        current = current and secrets.compare_digest(self.current(), commit)
        eligible = eligible and current
        deployments: tuple[LiteLlmDeployment, ...] = ()
        if current and eligible and routes:
            deployments = self._deployments(commit, routes)
            for deployment in deployments:
                if not isinstance(deployment, LiteLlmDeployment):
                    raise WorkerAuthorityError("repository deployment is invalid")
                LiteLlmPublisher._validate_hermes_deployment(deployment)
        return {
            "schema_version": 1,
            "commit": commit,
            "current": current,
            "eligible": eligible,
            "routes_sha256": hashlib.sha256(
                json.dumps(
                    [asdict(route) for route in routes],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
            "deployments": [asdict(item) for item in deployments],
        }

    def issued_at(self) -> int:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise WorkerAuthorityError("repository authority clock is invalid")
        return value


def worker_document_signature(token: bytes, document: Mapping[str, object]) -> str:
    """Return the HMAC for one canonical internal authority document."""

    if _TOKEN.fullmatch(token) is None:
        raise ValueError("worker authority token is invalid")
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.new(token, encoded, hashlib.sha256).hexdigest()


def install_worker_authority_routes(
    app: FastAPI,
    service: RepositoryAuthorityService,
    *,
    token: bytes,
) -> None:
    """Install worker-only routes guarded by an independent service token."""

    if _TOKEN.fullmatch(token) is None:
        raise ValueError("worker authority token is invalid")
    def authenticate(request: Request, document: Mapping[str, object]) -> None:
        supplied = request.headers.get("x-dgx-worker-signature", "")
        expected = worker_document_signature(token, document)
        if not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="authentication required")

    @app.post("/internal/v1/repository/evaluate", include_in_schema=False)
    def repository_evaluate(
        body: AuthorityRequest,
        request: Request,
    ) -> Mapping[str, object]:
        request_document = body.model_dump()
        authenticate(request, request_document)
        routes = tuple(PublishedRoute(**route.model_dump()) for route in body.routes)
        try:
            issued_at = service.issued_at()
            response = {
                **service.evaluate(body.commit, routes),
                "nonce": body.nonce,
                "issued_at": issued_at,
                "expires_at": issued_at + _MAX_ATTESTATION_SECONDS,
            }
            return {
                **response,
                "signature": worker_document_signature(token, response),
            }
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(status_code=503, detail="repository authority unavailable") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _open_without_redirect(request: urllib.request.Request, *, timeout: float):
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect,
    ).open(request, timeout=timeout)


class HttpWorkerAuthority:
    """Bounded fail-closed client used by the production worker."""

    def __init__(
        self,
        origin: str,
        token: bytes,
        *,
        timeout_seconds: float = 3.0,
        opener: Callable[..., object] = _open_without_redirect,
        clock: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        if not origin or origin.endswith("/") or _TOKEN.fullmatch(token) is None:
            raise ValueError("worker authority client configuration is invalid")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("worker authority timeout is invalid")
        self._origin = origin
        self._token = token
        self._timeout = timeout_seconds
        self._opener = opener
        self._clock = clock
        self._cached_commit: str | None = None
        self._cached_current = False

    def _request(
        self,
        path: str,
        *,
        document: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        content = None
        method = "GET"
        headers = {"Accept": "application/json"}
        if document is not None:
            content = json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            headers["Content-Type"] = "application/json"
            headers["X-DGX-Worker-Signature"] = worker_document_signature(
                self._token,
                document,
            )
            method = "POST"
        request = urllib.request.Request(
            self._origin + path,
            data=content,
            headers=headers,
            method=method,
        )
        try:
            response = self._opener(request, timeout=self._timeout)
            with response:
                status = getattr(response, "status", None)
                final_url = getattr(response, "geturl", lambda: request.full_url)()
                raw = response.read(_MAX_RESPONSE + 1)
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            raise WorkerAuthorityError("worker authority is unavailable") from error
        if (
            status != 200
            or final_url != request.full_url
            or len(raw) > _MAX_RESPONSE
        ):
            raise WorkerAuthorityError("worker authority rejected the request")
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as error:
            raise WorkerAuthorityError("worker authority response is invalid") from error
        if not isinstance(parsed, Mapping):
            raise WorkerAuthorityError("worker authority response is invalid")
        return parsed

    def current_commit(self) -> str:
        commit = self._cached_commit
        current = self._cached_current
        self._cached_commit = None
        self._cached_current = False
        if commit is None or not current:
            raise WorkerAuthorityError("repository commit is no longer current")
        return commit

    def _evaluate(
        self,
        commit: str,
        routes: tuple[PublishedRoute, ...],
    ) -> Mapping[str, object]:
        nonce = secrets.token_hex(16)
        request_document = {
            "schema_version": 1,
            "commit": commit,
            "nonce": nonce,
            "routes": [asdict(route) for route in routes],
        }
        document = self._request(
            "/internal/v1/repository/evaluate",
            document=request_document,
        )
        if set(document) != {
            "schema_version",
            "commit",
            "nonce",
            "current",
            "eligible",
            "routes_sha256",
            "deployments",
            "issued_at",
            "expires_at",
            "signature",
        }:
            raise WorkerAuthorityError("worker authority response is invalid")
        unsigned = dict(document)
        signature = unsigned.pop("signature")
        expected_signature = worker_document_signature(self._token, unsigned)
        expected_routes_digest = hashlib.sha256(
            json.dumps(
                request_document["routes"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        now = self._clock()
        if (
            document["schema_version"] != 1
            or document["commit"] != commit
            or document["nonce"] != nonce
            or not isinstance(document["current"], bool)
            or not isinstance(document["eligible"], bool)
            or not isinstance(document["deployments"], list)
            or document["routes_sha256"] != expected_routes_digest
            or not isinstance(signature, str)
            or not secrets.compare_digest(signature, expected_signature)
            or isinstance(document["issued_at"], bool)
            or not isinstance(document["issued_at"], int)
            or isinstance(document["expires_at"], bool)
            or not isinstance(document["expires_at"], int)
            or not document["issued_at"] <= now < document["expires_at"]
            or document["expires_at"] - document["issued_at"]
            > _MAX_ATTESTATION_SECONDS
        ):
            raise WorkerAuthorityError("worker authority response is invalid")
        return document

    def eligible(self, commit: str) -> bool:
        document = self._evaluate(commit, ())
        self._cached_commit = commit
        self._cached_current = document["current"] is True
        return document["eligible"] is True

    def deployments(
        self,
        commit: str,
        routes: tuple[PublishedRoute, ...],
    ) -> tuple[LiteLlmDeployment, ...]:
        document = self._evaluate(commit, routes)
        if document["current"] is not True or document["eligible"] is not True:
            raise WorkerAuthorityError("repository authority was lost")
        parsed: list[LiteLlmDeployment] = []
        try:
            for item in document["deployments"]:
                if not isinstance(item, Mapping) or set(item) != {
                    "model_name",
                    "workload",
                    "api_base",
                    "priority",
                    "requests_per_minute",
                    "tokens_per_minute",
                }:
                    raise TypeError
                deployment = LiteLlmDeployment(**item)
                LiteLlmPublisher._validate_hermes_deployment(deployment)
                parsed.append(deployment)
        except (TypeError, ValueError) as error:
            raise WorkerAuthorityError("worker authority deployments are invalid") from error
        if len({item.priority for item in parsed}) != len(parsed):
            raise WorkerAuthorityError("worker authority deployments are ambiguous")
        return tuple(parsed)
