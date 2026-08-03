import logging
from pathlib import Path

from dgx_control.logging import JobLogStore, log_event, redact_text


def test_structured_logger_redacts_secrets(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="test-control"):
        log_event(
            logging.getLogger("test-control"), "job.failed", service="control-worker",
            request_id="request", token="secret-value",
            stderr="Authorization: Bearer abc123 password=hunter2",
        )
    assert "secret-value" not in caplog.text
    assert "abc123" not in caplog.text
    assert "hunter2" not in caplog.text
    assert "<redacted>" in caplog.text


def test_job_log_store_is_content_addressed_bounded_and_sanitized(tmp_path: Path) -> None:
    store = JobLogStore(tmp_path)
    job_id = "00000000-0000-4000-8000-000000000001"
    digest = store.save(job_id, b"started\nAuthorization: Bearer no\nfinished\n")
    content = store.read(job_id, digest)
    assert b"started" in content and b"finished" in content
    assert b"Bearer no" not in content and b"<redacted>" in content
    assert (tmp_path / job_id / f"{digest}.log").stat().st_mode & 0o777 == 0o600


def test_redaction_truncates_remote_output() -> None:
    value = redact_text("x" * 100_000)
    assert len(value) <= 4096
    assert value.endswith("<truncated>")
