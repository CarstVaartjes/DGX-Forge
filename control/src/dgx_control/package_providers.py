"""Bounded metadata providers used by the generic package discovery service."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from spark_profiles.workload_packages import PackageFamily

from .package_discovery import DiscoveryCandidate, DiscoveryError, DiscoveryPage

MAX_RESPONSE_BYTES = 1024 * 1024
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")


@dataclass(frozen=True)
class MetadataResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


class MetadataTransport(Protocol):
    def request(self, url: str, headers: Mapping[str, str]) -> MetadataResponse: ...


class BoundedMetadataClient:
    """A tiny transport boundary: only bounded JSON metadata is read."""

    def __init__(
        self, transport: MetadataTransport, *, max_bytes: int = MAX_RESPONSE_BYTES
    ):
        if not 1024 <= max_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError("metadata response bound is invalid")
        self.transport = transport
        self.max_bytes = max_bytes

    def get(
        self, url: str, cursor: Mapping[str, object] | None = None
    ) -> MetadataResponse:
        _validate_url(url)
        request_headers: dict[str, str] = {"Accept": "application/json"}
        if cursor:
            etag = cursor.get("etag")
            modified = cursor.get("last_modified")
            upstream_cursor = cursor.get("upstream_cursor")
            if isinstance(etag, str) and etag:
                request_headers["If-None-Match"] = etag
            if isinstance(modified, str) and modified:
                request_headers["If-Modified-Since"] = modified
            if isinstance(upstream_cursor, str) and upstream_cursor:
                request_headers["X-Discovery-Cursor"] = upstream_cursor
        try:
            response = self.transport.request(url, request_headers)
        except DiscoveryError:
            raise
        except Exception as error:
            raise DiscoveryError(
                "discovery_unavailable", {"transport": type(error).__name__}
            ) from error
        if not isinstance(response, MetadataResponse):
            raise DiscoveryError("discovery_unavailable", {"response": "invalid"})
        if 300 <= response.status_code < 400:
            raise DiscoveryError("discovery_unavailable", {"redirect": "not permitted"})
        if response.status_code == 304:
            return response
        if response.status_code == 429:
            raise DiscoveryError("rate_limited", {"status": 429})
        if response.status_code >= 500:
            raise DiscoveryError(
                "discovery_unavailable", {"status": response.status_code}
            )
        if response.status_code != 200:
            raise DiscoveryError(
                "discovery_unavailable", {"status": response.status_code}
            )
        if (
            not isinstance(response.body, bytes)
            or not 0 < len(response.body) <= self.max_bytes
        ):
            raise DiscoveryError(
                "discovery_unavailable", {"reason": "metadata response exceeds bound"}
            )
        return response

    def json(
        self, url: str, cursor: Mapping[str, object] | None = None
    ) -> tuple[Mapping[str, object] | list[object] | None, MetadataResponse]:
        response = self.get(url, cursor)
        if response.status_code == 304:
            return None, response
        try:
            value = json.loads(
                response.body,
                object_pairs_hook=_unique_object,
                parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
            )
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DiscoveryError(
                "resolution_unsupported", {"reason": "metadata is not JSON"}
            ) from error
        if not isinstance(value, (Mapping, list)):
            raise DiscoveryError(
                "resolution_unsupported",
                {"reason": "metadata root is not an object or list"},
            )
        return value, response


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate metadata key: {key}")
        result[key] = value
    return result


def _validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise DiscoveryError("resolution_unsupported", {"reason": "source URL policy"})
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
    ):
        raise DiscoveryError(
            "resolution_unsupported", {"reason": "private source address"}
        )


def _cursor(response: MetadataResponse) -> Mapping[str, object] | None:
    value: dict[str, object] = {}
    if isinstance(response.headers.get("etag"), str):
        value["etag"] = response.headers["etag"]
    if isinstance(response.headers.get("last-modified"), str):
        value["last_modified"] = response.headers["last-modified"]
    next_cursor = response.headers.get(
        "x-next-cursor", response.headers.get("next-cursor")
    )
    if isinstance(next_cursor, str) and next_cursor:
        value["upstream_cursor"] = next_cursor
    return value or None


def _list(
    value: Mapping[str, object] | list[object], key: str
) -> list[Mapping[str, object]]:
    if isinstance(value, Mapping):
        items = value.get(key)
    else:
        items = value
    if not isinstance(items, list) or any(
        not isinstance(item, Mapping) for item in items
    ):
        raise DiscoveryError(
            "resolution_unsupported", {"reason": f"metadata.{key} layout"}
        )
    return [dict(item) for item in items]


def _text(value: object, field: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 256
        or any(ord(char) < 32 for char in value)
    ):
        raise DiscoveryError("resolution_unsupported", {"reason": f"invalid {field}"})
    return value.strip()


def _release_metadata(
    body: Mapping[str, object], entry: Mapping[str, object]
) -> dict[str, object]:
    # Keep upstream fields for a typed resolution recipe, while preserving the
    # provider's original metadata for audit/debugging.  Never include response
    # payload bytes; providers only pass JSON metadata through this function.
    return {**dict(body), "release": dict(entry), "source_metadata": dict(body)}


class _Provider:
    def __init__(self, client: BoundedMetadataClient):
        self.client = client

    def _page(
        self, response: MetadataResponse, candidates: list[DiscoveryCandidate]
    ) -> DiscoveryPage:
        return DiscoveryPage(
            tuple(
                sorted(candidates, key=lambda item: (item.upstream_version, item.id))
            ),
            _cursor(response),
            response.status_code == 304,
        )

    @staticmethod
    def _version(entry: Mapping[str, object], *, default: str | None = None) -> str:
        return (
            _text(
                entry.get("version", entry.get("tag", entry.get("name", default))),
                "version",
            )
            or ""
        )

    @staticmethod
    def _channel(entry: Mapping[str, object]) -> str:
        return _text(entry.get("channel", "stable"), "channel") or "stable"

    @staticmethod
    def _allowed(family: PackageFamily, version: str, channel: str) -> bool:
        channels = family.versions["channels"]
        if channel not in channels:
            return False
        return bool(family.versions["include_prereleases"]) or not (
            "-" in version or version.lower().endswith("dev")
        )


class GitReleaseProvider(_Provider):
    def discover(
        self, family: PackageFamily, cursor: Mapping[str, object] | None = None
    ) -> DiscoveryPage:
        body, response = self.client.json(str(family.source["locator"]), cursor)
        if body is None:
            return self._page(response, [])
        entries = (
            _list(body, "releases")
            if isinstance(body, Mapping) and "releases" in body
            else _list(body, "tags")
        )
        candidates: list[DiscoveryCandidate] = []
        for entry in entries:
            version = self._version(entry)
            channel = self._channel(entry)
            if not self._allowed(family, version, channel):
                continue
            commit = entry.get("commit", entry.get("target", entry.get("oid")))
            if (
                not isinstance(commit, str)
                or _COMMIT.fullmatch(commit) is None
                or commit.lower() != commit
            ):
                raise DiscoveryError(
                    "resolution_unsupported",
                    {"reason": "incomplete immutable Git identity"},
                )
            identity = {
                "provider": "git",
                "repository": str(family.source["locator"]),
                "commit": commit,
            }
            candidates.append(
                DiscoveryCandidate.create(
                    family_id=family.family_id,
                    release_key=str(entry.get("tag", entry.get("name", version))),
                    upstream_version=version,
                    channel=channel,
                    published_at=_text(
                        entry.get("published_at"), "published_at", required=False
                    ),
                    upstream_identity=identity,
                    metadata=_release_metadata(
                        body if isinstance(body, Mapping) else {}, entry
                    ),
                )
            )
        return self._page(response, candidates)


class OCIRegistryProvider(_Provider):
    def discover(
        self, family: PackageFamily, cursor: Mapping[str, object] | None = None
    ) -> DiscoveryPage:
        body, response = self.client.json(
            "https://" + str(family.source["locator"]), cursor
        )
        if body is None:
            return self._page(response, [])
        entries = _list(body, "tags")
        candidates: list[DiscoveryCandidate] = []
        for entry in entries:
            version = self._version(entry)
            channel = self._channel(entry)
            if not self._allowed(family, version, channel):
                continue
            digest = entry.get("digest")
            if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                raise DiscoveryError(
                    "resolution_unsupported",
                    {"reason": "OCI tag has no immutable digest"},
                )
            identity = {
                "provider": "oci",
                "reference": f"{family.source['locator']}@{digest}",
            }
            candidates.append(
                DiscoveryCandidate.create(
                    family_id=family.family_id,
                    release_key=str(entry.get("tag", version)),
                    upstream_version=version,
                    channel=channel,
                    published_at=_text(entry.get("created"), "created", required=False),
                    upstream_identity=identity,
                    metadata=_release_metadata(
                        body if isinstance(body, Mapping) else {}, entry
                    ),
                )
            )
        return self._page(response, candidates)


class HuggingFaceProvider(_Provider):
    def discover(
        self, family: PackageFamily, cursor: Mapping[str, object] | None = None
    ) -> DiscoveryPage:
        locator = str(family.source["locator"])
        body, response = self.client.json(
            f"https://huggingface.co/api/models/{locator}", cursor
        )
        if body is None:
            return self._page(response, [])
        if not isinstance(body, Mapping):
            raise DiscoveryError(
                "resolution_unsupported", {"reason": "Hugging Face metadata layout"}
            )
        revision = body.get("sha", body.get("commit"))
        if (
            not isinstance(revision, str)
            or _COMMIT.fullmatch(revision) is None
            or revision.lower() != revision
        ):
            raise DiscoveryError(
                "resolution_unsupported",
                {"reason": "Hugging Face revision is not immutable"},
            )
        version = self._version(body, default=revision)
        channel = self._channel(body)
        if not self._allowed(family, version, channel):
            return self._page(response, [])
        identity = {
            "provider": "huggingface",
            "repository": locator,
            "revision": revision,
        }
        candidate = DiscoveryCandidate.create(
            family_id=family.family_id,
            release_key=str(body.get("id", version)),
            upstream_version=version,
            channel=channel,
            published_at=_text(
                body.get("lastModified"), "lastModified", required=False
            ),
            upstream_identity=identity,
            metadata={
                **dict(body),
                "release": dict(body),
                "source_metadata": dict(body),
            },
        )
        return self._page(response, [candidate])


class PythonIndexProvider(_Provider):
    def discover(
        self, family: PackageFamily, cursor: Mapping[str, object] | None = None
    ) -> DiscoveryPage:
        locator = str(family.source["locator"])
        body, response = self.client.json(
            f"https://pypi.org/pypi/{locator}/json", cursor
        )
        if body is None:
            return self._page(response, [])
        entries = _list(body, "releases")
        candidates: list[DiscoveryCandidate] = []
        for entry in entries:
            version = self._version(entry)
            channel = self._channel(entry)
            if not self._allowed(family, version, channel):
                continue
            files = entry.get("files")
            if (
                not isinstance(files, list)
                or not files
                or any(not isinstance(item, Mapping) for item in files)
            ):
                raise DiscoveryError(
                    "incomplete_checksum_metadata", {"version": version}
                )
            normalized: list[dict[str, object]] = []
            for item in files:
                digest = item.get("digest", item.get("digests"))
                if isinstance(digest, Mapping):
                    digest = digest.get("sha256")
                if (
                    not isinstance(digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                    or not isinstance(item.get("size"), int)
                    or item["size"] <= 0
                ):
                    raise DiscoveryError(
                        "incomplete_checksum_metadata", {"version": version}
                    )
                url = item.get("url")
                _validate_url(str(url))
                normalized.append(
                    {
                        "filename": _text(item.get("filename"), "filename"),
                        "url": url,
                        "digest": "sha256:" + digest,
                        "size": item["size"],
                    }
                )
            identity_digest = hashlib.sha256(
                json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            identity = {
                "provider": "python-index",
                "project": locator,
                "version": version,
                "digest": "sha256:" + identity_digest,
            }
            candidates.append(
                DiscoveryCandidate.create(
                    family_id=family.family_id,
                    release_key=version,
                    upstream_version=version,
                    channel=channel,
                    published_at=_text(
                        entry.get("upload_time"), "upload_time", required=False
                    ),
                    upstream_identity=identity,
                    metadata={"release": dict(entry), "files": normalized},
                )
            )
        return self._page(response, candidates)


class SignedHTTPIndexProvider(_Provider):
    def discover(
        self, family: PackageFamily, cursor: Mapping[str, object] | None = None
    ) -> DiscoveryPage:
        body, response = self.client.json(str(family.source["locator"]), cursor)
        if body is None:
            return self._page(response, [])
        if not isinstance(body, Mapping) or not isinstance(
            body.get("signature"), Mapping
        ):
            raise DiscoveryError(
                "trust_or_provenance_failure",
                {"reason": "signed index signature missing"},
            )
        entries = _list(body, "releases")
        candidates: list[DiscoveryCandidate] = []
        for entry in entries:
            version = self._version(entry)
            channel = self._channel(entry)
            if not self._allowed(family, version, channel):
                continue
            digest = entry.get("digest", entry.get("sha256"))
            if isinstance(digest, str) and not digest.startswith("sha256:"):
                digest = "sha256:" + digest
            if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                raise DiscoveryError(
                    "incomplete_checksum_metadata", {"version": version}
                )
            identity = {
                "provider": "signed-http-index",
                "url": str(family.source["locator"]),
                "digest": digest,
            }
            candidates.append(
                DiscoveryCandidate.create(
                    family_id=family.family_id,
                    release_key=str(entry.get("name", version)),
                    upstream_version=version,
                    channel=channel,
                    published_at=_text(
                        entry.get("published_at"), "published_at", required=False
                    ),
                    upstream_identity=identity,
                    metadata={
                        **dict(body),
                        "release": dict(entry),
                        "signature": dict(body["signature"]),
                    },
                )
            )
        return self._page(response, candidates)


# Short aliases make provider registration readable while retaining explicit
# names for API documentation and future provider-specific options.
GitProvider = GitReleaseProvider
OCIProvider = OCIRegistryProvider
HuggingFaceReleaseProvider = HuggingFaceProvider
PythonProvider = PythonIndexProvider
SignedIndexProvider = SignedHTTPIndexProvider

__all__ = [
    "BoundedMetadataClient",
    "GitProvider",
    "GitReleaseProvider",
    "HuggingFaceProvider",
    "HuggingFaceReleaseProvider",
    "MetadataResponse",
    "MetadataTransport",
    "OCIProvider",
    "OCIRegistryProvider",
    "PythonIndexProvider",
    "PythonProvider",
    "SignedHTTPIndexProvider",
    "SignedIndexProvider",
]
