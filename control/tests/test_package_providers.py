from __future__ import annotations

import json

import pytest
from dgx_control.package_discovery import DiscoveryError
from dgx_control.package_providers import (
    BoundedMetadataClient,
    GitReleaseProvider,
    HuggingFaceProvider,
    MetadataResponse,
    OCIRegistryProvider,
    PythonIndexProvider,
    SignedHTTPIndexProvider,
)

from spark_profiles.workload_packages import PackageFamily


def _family(provider: str, locator: str) -> PackageFamily:
    return PackageFamily.load(
        {
            "schema_version": 1,
            "family_id": "synthetic-runtime",
            "source": {"provider": provider, "locator": locator, "policy_refs": []},
            "versions": {
                "scheme": "semver",
                "channels": ["stable"],
                "include_prereleases": False,
            },
            "discovery": {
                "poll_interval_seconds": 60,
                "bindings": [
                    {
                        "target": "upstream_identity.commit",
                        "source": "release.commit",
                        "value_type": "git-commit",
                        "required": True,
                    }
                ],
            },
            "resolution": {
                "recipe_version": 1,
                "components": [
                    {
                        "name": "runtime",
                        "kind": "artifact",
                        "media_type": "application/octet-stream",
                        "materialization": "file",
                        "platforms": ["linux/arm64"],
                    }
                ],
                "dependencies": [],
            },
            "policy": {"required_evidence": ["checksum"], "license_policy_refs": []},
            "compatibility": {
                "architectures": ["linux-arm64"],
                "operating_systems": ["linux"],
                "min_memory_bytes": 1,
                "min_storage_bytes": 1,
            },
            "execution": {"backend": "native", "adapter_abi": 1},
            "validation": [{"kind": "health", "timeout_seconds": 30}],
            "retention": {"release_count": 3, "rollback_count": 1},
        }
    )


class FakeTransport:
    def __init__(self, body: object, *, status_code: int = 200, headers=None):
        self.response = MetadataResponse(
            status_code=status_code,
            headers=headers or {},
            body=json.dumps(body).encode(),
        )
        self.requests: list[tuple[str, dict[str, str]]] = []

    def request(self, url: str, headers: dict[str, str]) -> MetadataResponse:
        self.requests.append((url, headers))
        return self.response


def _release(commit: str = "a" * 40) -> dict[str, object]:
    return {
        "version": "1.2.3",
        "channel": "stable",
        "commit": commit,
        "digest": "a" * 64,
        "published_at": "2026-08-06T10:00:00Z",
    }


def test_git_provider_uses_conditional_metadata_requests_and_never_payload_urls() -> (
    None
):
    transport = FakeTransport(
        {"releases": [_release()]},
        headers={"etag": '"v1"', "last-modified": "Wed, 06 Aug 2026 10:00:00 GMT"},
    )
    provider = GitReleaseProvider(BoundedMetadataClient(transport))
    page = provider.discover(
        _family("git", "https://git.example/project.git"),
        {"etag": '"old"', "cursor": "x"},
    )

    assert page.candidates[0].upstream_identity["commit"] == "a" * 40
    assert page.next_cursor == {
        "etag": '"v1"',
        "last_modified": "Wed, 06 Aug 2026 10:00:00 GMT",
    }
    assert transport.requests[0][1]["If-None-Match"] == '"old"'
    assert all("payload" not in url for url, _ in transport.requests)


def test_oci_provider_requires_immutable_digest_and_rejects_tag_only_entries() -> None:
    transport = FakeTransport(
        {
            "tags": [
                {"tag": "1.2.3", "digest": "sha256:" + "a" * 64, "size": 12},
                {"tag": "latest"},
            ]
        }
    )
    provider = OCIRegistryProvider(BoundedMetadataClient(transport))
    family = _family("oci", "registry.example/project")

    with pytest.raises(DiscoveryError, match="immutable"):
        provider.discover(family, None)


def test_huggingface_provider_extracts_full_revision_without_fetching_files() -> None:
    transport = FakeTransport(
        {
            "sha": "a" * 40,
            "lastModified": "2026-08-06T10:00:00Z",
            "siblings": [
                {
                    "rfilename": "weights.safetensors",
                    "size": 32,
                    "lfs": {"oid": "b" * 64, "size": 32},
                }
            ],
        }
    )
    provider = HuggingFaceProvider(BoundedMetadataClient(transport))
    page = provider.discover(_family("huggingface", "org/repository"), None)

    assert page.candidates[0].upstream_identity == {
        "provider": "huggingface",
        "repository": "org/repository",
        "revision": "a" * 40,
    }
    assert (
        page.candidates[0].metadata["siblings"][0]["rfilename"] == "weights.safetensors"
    )


def test_python_index_provider_requires_hash_for_each_release_file() -> None:
    body = {
        "releases": [
            {
                "version": "1.2.3",
                "files": [
                    {
                        "filename": "runtime.whl",
                        "url": "https://files.example/runtime.whl",
                        "digests": {"sha256": "a" * 64},
                        "size": 42,
                    }
                ],
            }
        ]
    }
    page = PythonIndexProvider(BoundedMetadataClient(FakeTransport(body))).discover(
        _family("python-index", "runtime"), None
    )
    assert page.candidates[0].upstream_identity["digest"].startswith("sha256:")

    missing = {
        "releases": [
            {
                "version": "1.2.4",
                "files": [
                    {
                        "filename": "runtime.whl",
                        "url": "https://files.example/runtime.whl",
                        "size": 42,
                    }
                ],
            }
        ]
    }
    with pytest.raises(DiscoveryError, match="checksum"):
        PythonIndexProvider(BoundedMetadataClient(FakeTransport(missing))).discover(
            _family("python-index", "runtime"), None
        )


def test_signed_index_rejects_oversized_metadata_and_requires_signed_document() -> None:
    body = {"releases": [_release()], "signature": {"keyid": "k", "sig": "s"}}
    page = SignedHTTPIndexProvider(BoundedMetadataClient(FakeTransport(body))).discover(
        _family("signed-http-index", "https://releases.example/index.json"), None
    )
    assert page.candidates[0].metadata["signature"] == {"keyid": "k", "sig": "s"}

    unsigned = {"releases": [_release()]}
    with pytest.raises(DiscoveryError, match="signature"):
        SignedHTTPIndexProvider(
            BoundedMetadataClient(FakeTransport(unsigned))
        ).discover(
            _family("signed-http-index", "https://releases.example/index.json"), None
        )
