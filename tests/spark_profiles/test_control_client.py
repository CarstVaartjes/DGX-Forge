import json
from pathlib import Path

import pytest

from spark_profiles.cli import main
from spark_profiles.control_client import ControlClient, ControlClientError


class Response:
    def __init__(self, payload, status=200):
        self._content = json.dumps(payload).encode()
        self.status = status
        self.headers = {"content-type": "application/json"}

    def read(self, size=-1):
        if size < 0:
            return self._content
        value, self._content = self._content[:size], self._content[size:]
        return value

    def __enter__(self): return self
    def __exit__(self, *_): pass


def test_client_reads_token_file_and_sends_canonical_proposal(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("signed-token\n")
    calls = []
    def opener(request, timeout):
        calls.append((request, timeout))
        return Response({"digest": "abc", "patch": "diff"})
    client = ControlClient("https://control.invalid", token, opener=opener)
    result = client.create_proposal({"base_commit": "base", "changes": [{"path": "inventory/fleet.toml", "document": {"schema_version": 2}}]})
    request = calls[0][0]
    assert request.full_url == "https://control.invalid/api/v1/proposals"
    assert request.headers["Authorization"] == "Bearer signed-token"
    assert json.loads(request.data) == {"base_commit": "base", "changes": [{"document": {"schema_version": 2}, "path": "inventory/fleet.toml"}]}
    assert result == {"digest": "abc", "patch": "diff"}


def test_client_rejects_symlink_token(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.write_text("token")
    link = tmp_path / "token"
    link.symlink_to(actual)
    with pytest.raises(ControlClientError, match="non-symlink"):
        ControlClient("https://control.invalid", link)


class FakeAdminClient:
    def __init__(self): self.payload = None
    def create_proposal(self, payload):
        self.payload = payload
        return {"digest": "same", "patch": "canonical"}


def test_sparkctl_admin_proposal_is_thin_api_adapter(tmp_path: Path, capsys) -> None:
    change = tmp_path / "change.json"
    change.write_text(json.dumps({"base_commit": "a" * 40, "changes": [{"path": "inventory/fleet.toml", "document": {"schema_version": 2}}]}))
    client = FakeAdminClient()
    assert main(["admin", "proposal", "--file", str(change), "--json"], control_client=client) == 0
    assert json.loads(capsys.readouterr().out) == {"digest": "same", "patch": "canonical"}
    assert client.payload["base_commit"] == "a" * 40
