"""Runtime settings, read from environment variables.

Secrets (API_KEY and the password in DATABASE_URL) are only ever read from
the environment and are excluded from `repr`. Relative paths resolve from
the working directory, which is the project root locally and /app in Docker.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    crm_csv_path: Path
    billing_xml_path: Path
    log_level: str = "INFO"
    log_file: Path | None = None
    log_format: str = "text"                # "text" or "json"
    api_key: str | None = field(default=None, repr=False)
    cors_origins: tuple[str, ...] = ()
    db_timeout_seconds: int = 10            # connect and pool-checkout timeout
    db_statement_timeout_ms: int = 30_000   # PostgreSQL per-statement limit
    stale_run_minutes: int = 30             # a 'running' run older than this is treated as dead

    @classmethod
    def from_env(cls) -> "Settings":
        log_file = os.getenv("LOG_FILE", "logs/pipeline.log")
        origins = os.getenv("CORS_ORIGINS", "")
        return cls(
            database_url=os.getenv("DATABASE_URL", "sqlite:///customers.db"),
            crm_csv_path=Path(os.getenv("CRM_CSV_PATH", "data/crm_customers.csv")),
            billing_xml_path=Path(os.getenv("BILLING_XML_PATH", "data/billing_customers.xml")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            log_file=Path(log_file) if log_file else None,
            log_format=os.getenv("LOG_FORMAT", "text").lower(),
            api_key=os.getenv("API_KEY") or None,
            cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
            db_timeout_seconds=_int_env("DB_TIMEOUT_SECONDS", 10),
            db_statement_timeout_ms=_int_env("DB_STATEMENT_TIMEOUT_MS", 30_000),
            stale_run_minutes=_int_env("STALE_RUN_MINUTES", 30),
        )
