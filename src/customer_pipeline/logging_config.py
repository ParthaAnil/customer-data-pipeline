"""Logging setup shared by the CLI and the API.

Every record carries the current request id (``-`` outside a request), which
the API also returns in the ``X-Request-ID`` header, so a request can be
traced through the logs. ``fmt="json"`` emits one JSON object per line for
log collectors.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from pathlib import Path

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

TEXT_FORMAT = "%(asctime)s %(levelname)-5s %(name)s [%(request_id)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": self.formatTime(record, DATE_FORMAT),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def configure_logging(level: str = "INFO", log_file: Path | None = None, fmt: str = "text") -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    formatter = JsonFormatter() if fmt == "json" else logging.Formatter(TEXT_FORMAT, DATE_FORMAT)
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(RequestIdFilter())

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=handlers,
        force=True,  # replace handlers installed earlier, e.g. by uvicorn
    )
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
