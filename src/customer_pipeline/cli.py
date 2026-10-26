"""Command-line interface.

    customer-pipeline run [--csv PATH] [--xml PATH] [--db URL]
    customer-pipeline serve [--host HOST] [--port PORT] [--db URL]

`run` exit codes: 0 success, 1 unexpected failure, 2 bad input file,
3 another run in progress.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path

from .config import Settings
from .database import init_db, make_engine, make_session_factory, wait_for_db
from .extractors import CsvCustomerExtractor, ExtractionError, XmlCustomerExtractor
from .logging_config import configure_logging
from .pipeline import CustomerPipeline
from .repository import RunAlreadyInProgress

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT, EXIT_BUSY = 0, 1, 2, 3


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def cmd_run(settings: Settings) -> int:
    engine = make_engine(
        settings.database_url, settings.db_timeout_seconds, settings.db_statement_timeout_ms
    )
    try:
        wait_for_db(engine)
        init_db(engine)
        pipeline = CustomerPipeline(
            extractors=[
                CsvCustomerExtractor(settings.crm_csv_path),
                XmlCustomerExtractor(settings.billing_xml_path),
            ],
            session_factory=make_session_factory(engine),
            stale_after=timedelta(minutes=settings.stale_run_minutes),
        )
        report = pipeline.run()
    except ExtractionError as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except RunAlreadyInProgress as exc:
        print(f"Skipped: {exc}", file=sys.stderr)
        return EXIT_BUSY
    except Exception as exc:
        print(f"Pipeline failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    finally:
        engine.dispose()
    print(json.dumps(asdict(report), indent=2))
    return EXIT_OK


def cmd_serve(settings: Settings, host: str, port: int) -> int:
    # Fail closed: an unauthenticated API may only listen on this machine.
    if not settings.api_key and not is_loopback(host):
        print(f"Refusing to listen on {host} without API_KEY set. "
              "Set API_KEY, or bind to 127.0.0.1 for local development.", file=sys.stderr)
        return EXIT_FAILED

    import uvicorn

    from .api import create_app

    uvicorn.run(
        create_app(settings),
        host=host,
        port=port,
        log_config=None,
        access_log=False,              # requests are logged by the app middleware
        server_header=False,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=20,
    )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="customer-pipeline", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="extract, clean, validate, merge and load once")
    run_p.add_argument("--csv", type=Path, help="CRM CSV export (overrides CRM_CSV_PATH)")
    run_p.add_argument("--xml", type=Path, help="billing XML export (overrides BILLING_XML_PATH)")
    run_p.add_argument("--db", help="database URL (overrides DATABASE_URL)")

    serve_p = sub.add_parser("serve", help="start the REST API")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8000)
    serve_p.add_argument("--db", help="database URL (overrides DATABASE_URL)")

    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if args.db:
        settings = replace(settings, database_url=args.db)
    configure_logging(settings.log_level, settings.log_file, settings.log_format)

    if args.command == "run":
        if args.csv:
            settings = replace(settings, crm_csv_path=args.csv)
        if args.xml:
            settings = replace(settings, billing_xml_path=args.xml)
        return cmd_run(settings)
    return cmd_serve(settings, args.host, args.port)


if __name__ == "__main__":
    sys.exit(main())
