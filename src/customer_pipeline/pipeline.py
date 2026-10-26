"""Pipeline orchestration: extract -> clean -> validate -> merge -> load.

A run is split into `start()` (take the run lock, create the run row) and
`execute()` (do the work) so the API can respond immediately and execute in
the background. Loading happens in a single transaction; on failure it is
rolled back and the failure is recorded on the run row, which also releases
the lock.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.orm import sessionmaker

from . import metrics
from .cleaner import RecordCleaner
from .extractors import BaseExtractor
from .merger import CustomerMerger
from .repository import CustomerRepository
from .validator import RecordValidator

logger = logging.getLogger(__name__)


@dataclass
class PipelineReport:
    run_id: int
    status: str
    records_read: int = 0
    records_valid: int = 0
    records_rejected: int = 0
    customers_created: int = 0
    customers_updated: int = 0
    duration_seconds: float = 0.0
    per_source: dict[str, int] = field(default_factory=dict)
    error: str | None = None


class CustomerPipeline:
    def __init__(
        self,
        extractors: list[BaseExtractor],
        session_factory: sessionmaker,
        cleaner: RecordCleaner | None = None,
        validator: RecordValidator | None = None,
        merger: CustomerMerger | None = None,
        stale_after: timedelta = timedelta(minutes=30),
    ) -> None:
        self.extractors = extractors
        self.session_factory = session_factory
        self.cleaner = cleaner or RecordCleaner()
        self.validator = validator or RecordValidator()
        self.merger = merger or CustomerMerger()
        self.stale_after = stale_after

    def run(self) -> PipelineReport:
        return self.execute(self.start())

    def start(self) -> int:
        """Acquire the run lock and return the new run id. Raises RunAlreadyInProgress."""
        with self.session_factory() as session:
            repo = CustomerRepository(session)
            repo.expire_stale_runs(self.stale_after)
            session.commit()
            run = repo.start_run()
            session.commit()
            logger.info("run=%d started", run.id)
            return run.id

    def execute(self, run_id: int) -> PipelineReport:
        started = time.perf_counter()
        report = PipelineReport(run_id=run_id, status="running")

        try:
            with self.session_factory() as session:
                repo = CustomerRepository(session)
                run = repo.get_run(run_id)

                raw_records = []
                for extractor in self.extractors:
                    batch = extractor.extract()
                    report.per_source[extractor.source_name] = len(batch)
                    raw_records.extend(batch)
                report.records_read = len(raw_records)

                cleaned = self.cleaner.clean_all(raw_records)
                valid, rejected = self.validator.validate_all(cleaned)
                report.records_valid, report.records_rejected = len(valid), len(rejected)

                golden = self.merger.merge(valid)
                report.customers_created, report.customers_updated = repo.upsert_customers(
                    golden, run_id=run_id
                )
                repo.save_rejects(run, rejected)

                self._copy_counts(run, report)
                repo.finish_run(run, "success")
                session.commit()
                report.status = "success"

        except Exception as exc:
            logger.exception("run=%d failed: %s", run_id, exc)
            report.status, report.error = "failed", f"{exc.__class__.__name__}: {exc}"
            with self.session_factory() as session:
                repo = CustomerRepository(session)
                run = repo.get_run(run_id)
                self._copy_counts(run, report)
                repo.finish_run(run, "failed", error=report.error)
                session.commit()
            raise
        finally:
            report.duration_seconds = round(time.perf_counter() - started, 3)
            metrics.PIPELINE_RUNS.labels(report.status).inc()
            metrics.PIPELINE_DURATION.observe(report.duration_seconds)
            metrics.PIPELINE_RECORDS.labels("valid").inc(report.records_valid)
            metrics.PIPELINE_RECORDS.labels("rejected").inc(report.records_rejected)

        logger.info(
            "run=%d success: read=%d valid=%d rejected=%d created=%d updated=%d (%.2fs)",
            run_id, report.records_read, report.records_valid, report.records_rejected,
            report.customers_created, report.customers_updated, report.duration_seconds,
        )
        return report

    def execute_in_background(self, run_id: int) -> None:
        """Background-task entry point; failures are already logged and stored on the run."""
        try:
            self.execute(run_id)
        except Exception:
            logger.debug("run=%d: background execution ended with an error", run_id)

    @staticmethod
    def _copy_counts(run, report: PipelineReport) -> None:
        run.records_read = report.records_read
        run.records_valid = report.records_valid
        run.records_rejected = report.records_rejected
        run.customers_created = report.customers_created
        run.customers_updated = report.customers_updated
