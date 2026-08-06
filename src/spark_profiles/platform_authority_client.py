"""OIDC client for the delegated online platform release authority."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_TARGET = re.compile(
    r"platform/releases/(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)/[0-9a-f]{64}\.json\Z"
)
_CHANNEL = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_MANIFEST_BYTES = 1024 * 1024


class AuthorityError(RuntimeError):
    """The delegated authority rejected or contradicted publication state."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


Transport = Callable[[str, str, dict[str, str], bytes | None], HttpResponse]


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _parse_object(raw: bytes, label: str) -> dict[str, Any]:
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise AuthorityError(f"{label} exceeds its size bound")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuthorityError(f"{label} is invalid") from error
    if not isinstance(value, dict) or raw != _canonical(value):
        raise AuthorityError(f"{label} is invalid")
    return value


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> HttpResponse:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as error:
        raw = error.read(_MAX_RESPONSE_BYTES + 1)
        return HttpResponse(error.code, dict(error.headers.items()), raw)
    except (OSError, urllib.error.URLError) as error:
        raise AuthorityError("delegated authority is unavailable") from error
    with response:
        raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise AuthorityError("delegated authority response exceeds its size bound")
        return HttpResponse(response.status, dict(response.headers.items()), raw)


def github_actions_oidc_token(
    request_url: str,
    request_token: str,
    audience: str,
    *,
    transport: Transport = urllib_transport,
) -> str:
    parsed = urllib.parse.urlsplit(request_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.fragment:
        raise AuthorityError("GitHub OIDC request URL is invalid")
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("audience", audience))
    url = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), "")
    )
    response = transport(
        "GET", url, {"Authorization": f"Bearer {request_token}"}, None
    )
    if response.status != 200:
        raise AuthorityError("GitHub OIDC token request failed")
    value = _parse_object(response.body, "GitHub OIDC response").get("value")
    if not isinstance(value, str) or not 32 <= len(value) <= 16 * 1024:
        raise AuthorityError("GitHub OIDC response is invalid")
    return value


class DelegatedPlatformAuthority:
    """Publish through one HTTPS service authenticated by short-lived OIDC."""

    def __init__(
        self,
        base_url: str,
        *,
        token: Callable[[], str],
        transport: Transport = urllib_transport,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise AuthorityError("delegated authority URL is invalid")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        token = self._token()
        if not isinstance(token, str) or not token:
            raise AuthorityError("delegated authority token is invalid")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _target_url(self, target_name: str) -> str:
        if _TARGET.fullmatch(target_name) is None:
            raise AuthorityError("immutable target name is invalid")
        return (
            self._base_url
            + "/v1/platform/targets/"
            + urllib.parse.quote(target_name, safe="")
        )

    def publish_target(
        self,
        target_name: str,
        target_sha256: str,
        manifest: bytes,
        retained_targets: list[str],
    ) -> dict[str, object]:
        if (
            not isinstance(manifest, bytes)
            or not 0 < len(manifest) <= _MAX_MANIFEST_BYTES
            or _SHA256.fullmatch(target_sha256) is None
            or hashlib.sha256(manifest).hexdigest() != target_sha256
            or any(_TARGET.fullmatch(item) is None for item in retained_targets)
            or len(set(retained_targets)) != len(retained_targets)
        ):
            raise AuthorityError("target publication request is invalid")
        url = self._target_url(target_name)
        request = {
            "manifest_base64": base64.b64encode(manifest).decode("ascii"),
            "retained_targets": retained_targets,
            "schema_version": 1,
            "target_name": target_name,
            "target_sha256": target_sha256,
        }
        headers = self._headers() | {
            "Idempotency-Key": target_sha256,
            "If-None-Match": "*",
        }
        response = self._transport("PUT", url, headers, _canonical(request))
        if response.status in {409, 412}:
            response = self._transport("GET", url, self._headers(), None)
        if response.status not in {200, 201}:
            raise AuthorityError("delegated target publication failed")
        receipt = _parse_object(response.body, "target publication receipt")
        if (
            receipt.get("target_name") != target_name
            or receipt.get("target_sha256") != target_sha256
            or receipt.get("retained_targets") != retained_targets
            or type(receipt.get("targets_version")) is not int
            or receipt["targets_version"] < 1
        ):
            raise AuthorityError("target publication receipt does not match request")
        return receipt

    def publish_channel(self, channel: str, document_raw: bytes) -> dict[str, object]:
        if _CHANNEL.fullmatch(channel) is None or channel == "latest":
            raise AuthorityError("channel name is invalid")
        document = _parse_object(document_raw, "channel document")
        if (
            document.get("channel") != channel
            or document.get("discovery_only") is not True
            or type(document.get("tuf_targets_version")) is not int
            or document["tuf_targets_version"] < 1
            or not isinstance(document.get("target_name"), str)
            or _TARGET.fullmatch(document["target_name"]) is None
            or not isinstance(document.get("target_sha256"), str)
            or _SHA256.fullmatch(document["target_sha256"]) is None
        ):
            raise AuthorityError("channel document is invalid")
        url = self._base_url + "/v1/platform/channels/" + channel
        current = self._transport("GET", url, self._headers(), None)
        if current.status == 200:
            current_document = _parse_object(current.body, "current channel document")
            if current.body == document_raw:
                return self._channel_receipt(channel, document_raw, document)
            current_version = current_document.get("tuf_targets_version")
            if (
                type(current_version) is not int
                or document["tuf_targets_version"] <= current_version
            ):
                raise AuthorityError("channel publication must advance targets version")
            etag = current.headers.get("ETag")
            if not isinstance(etag, str) or not etag:
                raise AuthorityError("current channel CAS token is missing")
            cas = {"If-Match": etag}
        elif current.status == 404:
            cas = {"If-None-Match": "*"}
        else:
            raise AuthorityError("current channel cannot be read")
        response = self._transport(
            "PUT", url, self._headers() | cas, document_raw
        )
        if response.status not in {200, 201}:
            raise AuthorityError("channel CAS publication failed")
        receipt = _parse_object(response.body, "channel publication receipt")
        expected = self._channel_receipt(channel, document_raw, document)
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise AuthorityError("channel publication receipt does not match request")
        return receipt

    @staticmethod
    def _channel_receipt(
        channel: str, raw: bytes, document: dict[str, Any]
    ) -> dict[str, object]:
        return {
            "channel": channel,
            "document_sha256": hashlib.sha256(raw).hexdigest(),
            "target_name": document["target_name"],
            "target_sha256": document["target_sha256"],
        }
