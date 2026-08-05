"""Bounded HTTPS client for normal control-plane administration."""

from __future__ import annotations

import json
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from .generated_control.api.default import (
    apply_reconciliation,
    get_job,
    get_node_statuses,
    get_published_endpoint,
    list_agents,
    plan_profile_reconciliation,
)
from .generated_control.client import AuthenticatedClient
from .generated_control.models.agents_response import AgentsResponse
from .generated_control.models.endpoint_response import EndpointResponse
from .generated_control.models.fleet_status_response import FleetStatusResponse
from .generated_control.models.job_detail_response import JobDetailResponse
from .generated_control.models.reconciliation_accepted_response import (
    ReconciliationAcceptedResponse,
)
from .generated_control.models.reconciliation_plan_response import (
    ReconciliationPlanResponse,
)
from .generated_control.models.reconciliation_request import ReconciliationRequest
from .generated_control.types import Response as GeneratedResponse

_MAX_RESPONSE = 1_048_576


class ControlClientError(RuntimeError):
    pass


class ControlMalformedResponse(ControlClientError):
    pass


class ControlResponseTooLarge(ControlClientError):
    pass


class ControlTransportError(ControlClientError):
    pass


class ControlTimeout(ControlClientError):
    def __init__(self, job_id: str, job: JobDetailResponse | None) -> None:
        self.job_id = job_id
        self.job = job
        super().__init__(f"timed out waiting for control job {job_id}")


class JobTerminalError(ControlClientError):
    def __init__(self, job: JobDetailResponse) -> None:
        self.job = job
        self.reason = job.status_reason
        super().__init__(
            f"control job {job.id} entered {job.state}: "
            f"{job.status_reason or 'no reason provided'}"
        )


class JobFailed(JobTerminalError):
    pass


class JobWaitingForOperator(JobTerminalError):
    pass


class ControlHTTPError(ControlClientError):
    def __init__(
        self,
        status_code: int,
        detail: str,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.status_code = status_code
        self.detail = detail
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"control API returned HTTP {status_code}: {detail}")


class ControlUnauthorized(ControlHTTPError):
    pass


class ControlForbidden(ControlHTTPError):
    pass


class ControlNotFound(ControlHTTPError):
    pass


class ControlConflict(ControlHTTPError):
    pass


class ControlUnavailable(ControlHTTPError):
    pass


_STATUS_ERRORS: dict[int, type[ControlHTTPError]] = {
    401: ControlUnauthorized,
    403: ControlForbidden,
    404: ControlNotFound,
    409: ControlConflict,
    503: ControlUnavailable,
}


def _bounded_retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        seconds = int(value)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return max(1, min(30, seconds))


class _OpenerTransport(httpx.BaseTransport):
    def __init__(self, opener: Callable[..., object], timeout: float) -> None:
        self._opener = opener
        self._timeout = timeout

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        outgoing = urllib.request.Request(
            str(request.url),
            data=request.content or None,
            headers=dict(request.headers),
            method=request.method,
        )
        try:
            response_context = self._opener(outgoing, timeout=self._timeout)
        except urllib.error.HTTPError as error:
            response_context = error
        except (OSError, urllib.error.URLError) as error:
            raise ControlTransportError(
                f"control API request failed: {type(error).__name__}"
            ) from None
        with response_context as response:  # type: ignore[attr-defined]
            content = response.read(_MAX_RESPONSE + 1)  # type: ignore[attr-defined]
            if len(content) > _MAX_RESPONSE:
                raise ControlResponseTooLarge(
                    "control API response exceeds safety limit"
                )
            response_headers = httpx.Headers(response.headers)  # type: ignore[attr-defined]
            media_type = response_headers.get("content-type", "").split(";", 1)[0]
            if media_type.strip().lower() != "application/json":
                raise ControlMalformedResponse(
                    "control API returned an invalid content type"
                )
            return httpx.Response(
                response.status,  # type: ignore[attr-defined]
                content=content,
                headers=response_headers,
                request=request,
            )


class ControlClient:
    def __init__(
        self,
        base_url: str,
        token_file: Path,
        *,
        opener: Callable[..., object] = urllib.request.urlopen,
        timeout_seconds: float = 15,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ControlClientError(
                "control URL must be an HTTPS origin without credentials"
            )
        if token_file.is_symlink() or not token_file.is_file():
            raise ControlClientError("control token must be a regular non-symlink file")
        if stat.S_IMODE(token_file.stat().st_mode) & 0o077:
            raise ControlClientError("control token file permissions are too broad")
        token = token_file.read_text().strip()
        if (
            not token
            or len(token) > 8192
            or any(character.isspace() for character in token)
        ):
            raise ControlClientError("control token file is invalid")
        self._base = base_url.rstrip("/")
        self._token = token
        self._opener = opener
        self._timeout = timeout_seconds

    def _generated_client(
        self, headers: Mapping[str, str] | None = None
    ) -> AuthenticatedClient:
        return AuthenticatedClient(
            base_url=self._base,
            token=self._token,
            headers={"Accept": "application/json", **dict(headers or {})},
            timeout=httpx.Timeout(self._timeout),
            verify_ssl=True,
            follow_redirects=False,
            httpx_args={"transport": _OpenerTransport(self._opener, self._timeout)},
        )

    def _call_generated(
        self,
        operation: Callable[..., GeneratedResponse[Any]],
        *args: object,
        headers: Mapping[str, str] | None = None,
        **kwargs: object,
    ) -> object:
        try:
            with self._generated_client(headers) as client:
                response = operation(*args, client=client, **kwargs)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ControlMalformedResponse(
                "control API returned invalid JSON"
            ) from None
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ControlMalformedResponse(
                "control API response does not match the generated schema"
            ) from None
        if 200 <= response.status_code < 300 and response.parsed is not None:
            return response.parsed
        error_type = _STATUS_ERRORS.get(response.status_code)
        detail = getattr(response.parsed, "detail", "control API request failed")
        if error_type is not None:
            raise error_type(
                response.status_code,
                detail,
                _bounded_retry_after(response.headers.get("retry-after")),
            )
        raise ControlClientError(f"control API returned HTTP {response.status_code}")

    @classmethod
    def from_environment(cls) -> ControlClient:
        import os

        url = os.environ.get("DGX_CONTROL_URL", "")
        token = os.environ.get("DGX_CONTROL_TOKEN_FILE", "")
        if not url or not token:
            raise ControlClientError(
                "DGX_CONTROL_URL and DGX_CONTROL_TOKEN_FILE are required"
            )
        return cls(url, Path(token))

    def request(
        self, method: str, path: str, payload: Mapping[str, object] | None = None
    ) -> dict[str, object]:
        if not path.startswith("/api/v1/") or ".." in path:
            raise ControlClientError("control API path is invalid")
        data = None
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        if payload is not None:
            data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._base + path, data=data, headers=headers, method=method
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                content = response.read(_MAX_RESPONSE + 1)
                status = response.status
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as error:
            raise ControlClientError(
                f"control API request failed: {type(error).__name__}"
            ) from None
        if len(content) > _MAX_RESPONSE:
            raise ControlClientError("control API response exceeds safety limit")
        if not 200 <= status < 300:
            raise ControlClientError(f"control API returned HTTP {status}")
        try:
            decoded = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ControlClientError("control API returned invalid JSON") from None
        if not isinstance(decoded, dict):
            raise ControlClientError("control API response must be an object")
        return decoded

    def create_proposal(self, payload: Mapping[str, object]) -> dict[str, object]:
        return self.request("POST", "/api/v1/proposals", payload)

    def get(self, path: str) -> dict[str, object]:
        return self.request("GET", path)

    def submit_change(self, digest: str) -> dict[str, object]:
        return self.request("POST", "/api/v1/changes", {"proposal_digest": digest})

    def nodes(self) -> FleetStatusResponse:
        return self._call_generated(get_node_statuses.sync_detailed)  # type: ignore[return-value]

    def plan_profile(self, profile: str) -> ReconciliationPlanResponse:
        return self._call_generated(
            plan_profile_reconciliation.sync_detailed, profile, body=None
        )  # type: ignore[return-value]

    def apply_plan(
        self, digest: str, *, request_id: str
    ) -> ReconciliationAcceptedResponse:
        try:
            canonical_request_id = str(uuid.UUID(request_id))
        except ValueError:
            raise ControlClientError("control mutation request ID is invalid") from None
        if canonical_request_id != request_id:
            raise ControlClientError("control mutation request ID is invalid")
        return self._call_generated(
            apply_reconciliation.sync_detailed,
            body=ReconciliationRequest(plan_digest=digest),
            headers={"X-Request-ID": canonical_request_id},
        )  # type: ignore[return-value]

    def job(self, job_id: str) -> JobDetailResponse:
        return self._call_generated(get_job.sync_detailed, job_id)  # type: ignore[return-value]

    def wait_job(
        self, job_id: str, timeout: float, interval: float
    ) -> JobDetailResponse:
        deadline = time.monotonic() + timeout
        result: JobDetailResponse | None = None
        while True:
            try:
                result = self.job(job_id)
            except (ControlTransportError, ControlUnavailable) as error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ControlTimeout(job_id, result) from error
                delay = getattr(error, "retry_after_seconds", None)
                if delay is None:
                    delay = interval
                if delay >= remaining:
                    time.sleep(remaining)
                    raise ControlTimeout(job_id, result) from error
                time.sleep(delay)
                continue
            if result.state == "succeeded":
                return result
            if result.state in {"expired", "failed"}:
                raise JobFailed(result)
            if result.state == "waiting-for-operator":
                raise JobWaitingForOperator(result)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ControlTimeout(job_id, result)
            if interval >= remaining:
                time.sleep(remaining)
                raise ControlTimeout(job_id, result)
            time.sleep(interval)

    def endpoint(self, alias: str) -> EndpointResponse:
        return self._call_generated(get_published_endpoint.sync_detailed, alias)  # type: ignore[return-value]

    def agents(self) -> AgentsResponse:
        return self._call_generated(list_agents.sync_detailed)  # type: ignore[return-value]
