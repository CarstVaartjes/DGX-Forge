from pathlib import Path

from scripts.vonk_identity import verify


def test_identity_verifier_rejects_owned_spark_token(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("vonk sparkctl\n", encoding="utf-8")

    result = verify(tmp_path)

    assert result["status"] == "failed"
    assert "sparkctl" in result["owned_matches"][0]["text"]
