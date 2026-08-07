"""Bounded anonymous client for immutable vonkforge.ai recipe revisions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .recipe_contract import RecipeContractError, recipe_content_sha256, validate_recipe

_URI = re.compile(
    r"^vonk://catalog/(?P<publisher>[a-z0-9][a-z0-9-]{1,62})/"
    r"(?P<slug>[a-z0-9][a-z0-9-]{1,62})@sha256:(?P<digest>[0-9a-f]{64})$"
)
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_MAX_RESPONSE_BYTES = 512 * 1024
_MAX_SOURCE_BUNDLE_BYTES = 64 * 1024 * 1024


class GlobalCatalogError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail[:256]
        super().__init__(self.detail)


@dataclass(frozen=True, slots=True)
class GlobalRecipeRevision:
    publisher: str
    slug: str
    recipe_id: str
    revision_number: int
    revision_id: str
    content_sha256: str
    published_at: str
    document: dict[str, object]

    @property
    def uri(self) -> str:
        return (
            f"vonk://catalog/{self.publisher}/{self.slug}@sha256:{self.content_sha256}"
        )


class GlobalCatalogClient:
    """Read public metadata without ambient credentials, redirects, or large bodies."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        parsed = urlsplit(base_url)
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise GlobalCatalogError(
                "global.url_insecure", "global catalog URL must use HTTPS"
            )
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise GlobalCatalogError(
                "global.url_invalid", "global catalog URL contains forbidden components"
            )
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def fetch(self, uri: str) -> GlobalRecipeRevision:
        match = _URI.fullmatch(uri)
        if match is None:
            raise GlobalCatalogError(
                "global.uri_invalid",
                "use vonk://catalog/PUBLISHER/SLUG@sha256:DIGEST",
            )
        publisher, slug, expected = (
            match.group("publisher"),
            match.group("slug"),
            match.group("digest"),
        )
        base = f"/v1/recipes/{quote(publisher, safe='')}/{quote(slug, safe='')}"
        revision = self._get_json(f"{base}/revisions/sha256/{expected}")
        result = self._revision(revision)
        if (
            result.publisher != publisher
            or result.slug != slug
            or result.content_sha256 != expected
            or recipe_content_sha256(result.document) != expected
        ):
            raise GlobalCatalogError(
                "global.revision_changed",
                "catalog revision content does not match the requested immutable hash",
            )
        try:
            validate_recipe(result.document)
        except RecipeContractError as error:
            raise GlobalCatalogError(
                "global.schema_incompatible",
                f"catalog recipe is incompatible at {error.path}",
            ) from error
        identity = result.document.get("identity")
        if not isinstance(identity, dict) or identity != {
            "publisher": publisher,
            "slug": slug,
        }:
            raise GlobalCatalogError(
                "global.identity_mismatch", "catalog recipe identity is inconsistent"
            )
        return result

    def fetch_source_bundle(self, sha256: str) -> bytes:
        if re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
            raise GlobalCatalogError(
                "global.source_digest_invalid", "source bundle digest is invalid"
            )
        try:
            with self._client.stream("GET", f"/v1/source-bundles/{sha256}") as response:
                if 300 <= response.status_code < 400:
                    raise GlobalCatalogError(
                        "global.redirect_forbidden",
                        "catalog redirects are not followed",
                    )
                if response.status_code == 404:
                    raise GlobalCatalogError(
                        "global.source_not_found", "catalog source bundle is not public"
                    )
                if response.status_code != 200:
                    raise GlobalCatalogError(
                        "global.unavailable", "global catalog source request failed"
                    )
                media_type = response.headers.get("content-type", "").split(";", 1)[0]
                if media_type != "application/vnd.vonk-forge.source-bundle.v1+tar":
                    raise GlobalCatalogError(
                        "global.response_invalid",
                        "catalog source response is not a Vonk bundle",
                    )
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_SOURCE_BUNDLE_BYTES:
                        raise GlobalCatalogError(
                            "global.response_too_large",
                            "catalog source bundle exceeds 64 MiB",
                        )
        except GlobalCatalogError:
            raise
        except (httpx.HTTPError, OSError) as error:
            raise GlobalCatalogError(
                "global.unavailable", "global catalog source is unavailable"
            ) from error
        if not body:
            raise GlobalCatalogError(
                "global.response_invalid", "catalog source bundle is empty"
            )
        return bytes(body)

    def _get_json(self, path: str) -> dict[str, Any]:
        try:
            with self._client.stream("GET", path) as response:
                if 300 <= response.status_code < 400:
                    raise GlobalCatalogError(
                        "global.redirect_forbidden",
                        "catalog redirects are not followed",
                    )
                if response.status_code == 404:
                    raise GlobalCatalogError(
                        "global.not_found", "catalog recipe is not public"
                    )
                if response.status_code != 200:
                    raise GlobalCatalogError(
                        "global.unavailable", "global catalog request failed"
                    )
                content_type = response.headers.get("content-type", "application/json")
                if content_type.split(";", 1)[0].strip().lower() != "application/json":
                    raise GlobalCatalogError(
                        "global.response_invalid", "catalog response is not JSON"
                    )
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_RESPONSE_BYTES:
                        raise GlobalCatalogError(
                            "global.response_too_large",
                            "catalog response exceeds 512 KiB",
                        )
        except GlobalCatalogError:
            raise
        except (httpx.HTTPError, OSError) as error:
            raise GlobalCatalogError(
                "global.unavailable",
                "global catalog is unavailable; local recipes are unaffected",
            ) from error
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GlobalCatalogError(
                "global.response_invalid", "catalog response is invalid JSON"
            ) from error
        if not isinstance(value, dict):
            raise GlobalCatalogError(
                "global.response_invalid", "catalog response must be an object"
            )
        return value

    @staticmethod
    def _revision(value: dict[str, Any]) -> GlobalRecipeRevision:
        document = value.get("document")
        fields = {
            "publisher": value.get("publisher"),
            "slug": value.get("slug"),
            "recipe_id": value.get("recipe_id"),
            "revision_id": value.get("revision_id"),
            "content_sha256": value.get("content_sha256"),
            "published_at": value.get("published_at"),
        }
        if (
            not isinstance(document, dict)
            or not isinstance(value.get("revision_number"), int)
            or any(not isinstance(item, str) or not item for item in fields.values())
            or _UUID.fullmatch(str(fields["recipe_id"])) is None
            or _UUID.fullmatch(str(fields["revision_id"])) is None
            or re.fullmatch(r"[0-9a-f]{64}", str(fields["content_sha256"])) is None
        ):
            raise GlobalCatalogError(
                "global.response_invalid", "catalog revision response is incomplete"
            )
        return GlobalRecipeRevision(
            publisher=fields["publisher"],
            slug=fields["slug"],
            recipe_id=fields["recipe_id"],
            revision_number=value["revision_number"],
            revision_id=fields["revision_id"],
            content_sha256=fields["content_sha256"],
            published_at=fields["published_at"],
            document=document,
        )


__all__ = ["GlobalCatalogClient", "GlobalCatalogError", "GlobalRecipeRevision"]
