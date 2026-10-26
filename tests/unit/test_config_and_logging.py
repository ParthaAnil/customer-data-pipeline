import json
import logging

from customer_pipeline.config import Settings
from customer_pipeline.logging_config import (
    JsonFormatter, RequestIdFilter, configure_logging, request_id_var,
)


def test_settings_read_from_env(monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret")
    monkeypatch.setenv("CORS_ORIGINS", "https://a.example, https://b.example ,")
    monkeypatch.setenv("DB_TIMEOUT_SECONDS", "3")
    s = Settings.from_env()
    assert s.api_key == "s3cret"
    assert s.cors_origins == ("https://a.example", "https://b.example")
    assert s.db_timeout_seconds == 3


def test_secrets_never_appear_in_repr(monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:hunter2@db/customers")
    text = repr(Settings.from_env())
    assert "s3cret" not in text
    assert "hunter2" not in text


def test_empty_api_key_means_none(monkeypatch):
    monkeypatch.setenv("API_KEY", "")
    assert Settings.from_env().api_key is None


def _record(msg="hello"):
    record = logging.LogRecord("customer_pipeline.x", logging.INFO, __file__, 1, msg, (), None)
    RequestIdFilter().filter(record)
    return record


def test_json_log_line_includes_request_id():
    token = request_id_var.set("req-42")
    try:
        line = json.loads(JsonFormatter().format(_record()))
    finally:
        request_id_var.reset(token)
    assert line["request_id"] == "req-42"
    assert line["message"] == "hello"
    assert line["level"] == "INFO"


def test_request_id_defaults_to_dash_outside_requests():
    assert _record().request_id == "-"


def test_configure_json_logging_writes_json_to_file(tmp_path):
    log_file = tmp_path / "app.log"
    configure_logging("INFO", log_file, fmt="json")
    logging.getLogger("customer_pipeline.test").info("structured")
    for h in logging.getLogger().handlers:
        h.flush()
    entry = json.loads(log_file.read_text().strip().splitlines()[-1])
    assert entry["message"] == "structured"
