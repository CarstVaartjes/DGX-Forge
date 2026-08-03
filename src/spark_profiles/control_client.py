"""Bounded HTTPS client for normal control-plane administration."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

_MAX_RESPONSE = 1_048_576


class ControlClientError(RuntimeError):
    pass


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
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ControlClientError("control URL must be an HTTPS origin without credentials")
        if token_file.is_symlink() or not token_file.is_file():
            raise ControlClientError("control token must be a regular non-symlink file")
        token = token_file.read_text().strip()
        if not token or len(token) > 8192 or any(character.isspace() for character in token):
            raise ControlClientError("control token file is invalid")
        self._base = base_url.rstrip("/")
        self._token = token
        self._opener = opener
        self._timeout = timeout_seconds

    @classmethod
    def from_environment(cls) -> "ControlClient":
        import os

        url = os.environ.get("DGX_CONTROL_URL", "")
        token = os.environ.get("DGX_CONTROL_TOKEN_FILE", "")
        if not url or not token:
            raise ControlClientError("DGX_CONTROL_URL and DGX_CONTROL_TOKEN_FILE are required")
        return cls(url, Path(token))

    def request(self, method: str, path: str, payload: Mapping[str, object] | None = None) -> dict[str, object]:
        if not path.startswith("/api/v1/") or ".." in path:
            raise ControlClientError("control API path is invalid")
        data = None
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self._base + path, data=data, headers=headers, method=method)
        try:
            with self._opener(request, timeout=self._timeout) as response:
                content = response.read(_MAX_RESPONSE + 1)
                status = response.status
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as error:
            raise ControlClientError(f"control API request failed: {type(error).__name__}") from None
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
