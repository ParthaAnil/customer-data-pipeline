"""Data access layer. All SQL lives here; values are always bound parameters."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import timedelta
from typing import Iterable, Iterator

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .cleaner import RecordCleaner
from .database import Customer, CustomerSource, PipelineRun, RejectedRow, utcnow
from .models import GoldenCustomer, RejectedRecord

logger = logging.getLogger(__name__)

UPDATABLE_FIELDS = ("first_name", "last_name", "email", "phone", "city", "country", "plan")
IN_CLAUSE_CHUNK = 500


class RunAlreadyInProgress(Exception):
    """Another pipeline run holds the run lock."""


def escape_like(term: str) -> str:
    r"""Escape LIKE wildcards so user input matches literally ('50%' must not match '500')."""
    return term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _chunks(items: list, size: int = IN_CLAUSE_CHUNK) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


class CustomerRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- writes -----------------------------------------------------------

    def upsert_customers(
        self, golden_list: list[GoldenCustomer], run_id: int | None = None
    ) -> tuple[int, int]:
        """Insert or update each golden customer. Returns (created, updated).

        Idempotent: reloading the same input changes nothing. Existing links
        and customers are prefetched in batches, so the number of SELECTs is
        independent of the number of customers.
        """
        links = self._prefetch_links(m for g in golden_list for m in g.members)
        by_email = self._prefetch_by_email(g.email for g in golden_list)

        created = updated = 0
        for golden in golden_list:
            customer = self._find_existing(golden, links, by_email)
            if customer is None:
                customer = Customer(email=golden.email)
                self.session.add(customer)
                created += 1
            else:
                updated += 1
            self._apply(customer, golden)
            self._link_sources(customer, golden, links, run_id)
            by_email[customer.email] = customer

        self.session.flush()
        return created, updated

    def _prefetch_links(self, members: Iterable) -> dict[tuple[str, str], CustomerSource]:
        wanted = {(m.source, m.source_id) for m in members}
        ids = sorted({source_id for _, source_id in wanted})
        found: dict[tuple[str, str], CustomerSource] = {}
        for chunk in _chunks(ids):
            rows = self.session.scalars(
                select(CustomerSource)
                .where(CustomerSource.source_id.in_(chunk))
                .options(selectinload(CustomerSource.customer))
            )
            for link in rows:
                key = (link.source, link.source_id)
                if key in wanted:
                    found[key] = link
        return found

    def _prefetch_by_email(self, emails: Iterable[str]) -> dict[str, Customer]:
        found: dict[str, Customer] = {}
        for chunk in _chunks(sorted(set(emails))):
            for customer in self.session.scalars(select(Customer).where(Customer.email.in_(chunk))):
                found[customer.email] = customer
        return found

    @staticmethod
    def _find_existing(golden, links, by_email) -> Customer | None:
        # A previously linked source record is the strongest signal; email is the fallback.
        for member in golden.members:
            link = links.get((member.source, member.source_id))
            if link is not None:
                return link.customer
        return by_email.get(golden.email)

    @staticmethod
    def _apply(customer: Customer, golden: GoldenCustomer) -> None:
        for name in UPDATABLE_FIELDS:
            new_value = getattr(golden, name)
            # Never blank out stored data; skip no-op writes so versions only move on real change.
            if new_value is not None and getattr(customer, name) != new_value:
                setattr(customer, name, new_value)
        if golden.customer_since and (
            customer.customer_since is None or golden.customer_since < customer.customer_since
        ):
            customer.customer_since = golden.customer_since

    @staticmethod
    def _link_sources(customer, golden, links, run_id) -> None:
        for member in golden.members:
            key = (member.source, member.source_id)
            link = links.get(key)
            if link is None:
                link = CustomerSource(source=member.source, source_id=member.source_id)
                customer.sources.append(link)
                links[key] = link
            link.last_seen_run_id = run_id

    # -- run lock ---------------------------------------------------------

    def expire_stale_runs(self, older_than: timedelta) -> int:
        """Fail runs left 'running' by a crashed process so they stop holding the lock."""
        cutoff = utcnow() - older_than
        result = self.session.execute(
            update(PipelineRun)
            .where(PipelineRun.status == "running", PipelineRun.started_at < cutoff)
            .values(status="failed", finished_at=utcnow(),
                    error=f"abandoned: still 'running' after {older_than}")
        )
        if result.rowcount:
            logger.warning("expired %d stale run(s) older than %s", result.rowcount, older_than)
        return result.rowcount

    def start_run(self) -> PipelineRun:
        run = PipelineRun(status="running")
        self.session.add(run)
        try:
            self.session.flush()
        except IntegrityError as exc:  # uq_one_running_run
            self.session.rollback()
            raise RunAlreadyInProgress("another pipeline run is already in progress") from exc
        return run

    def finish_run(self, run: PipelineRun, status: str, error: str | None = None) -> None:
        run.status = status
        run.error = error
        run.finished_at = utcnow()

    def save_rejects(self, run: PipelineRun, rejects: list[RejectedRecord]) -> None:
        for rej in rejects:
            raw = rej.record.raw or {
                k: v for k, v in asdict(rej.record).items() if k != "raw"
            }
            self.session.add(
                RejectedRow(
                    run_id=run.id,
                    source=rej.record.source,
                    source_id=rej.record.source_id,
                    reasons="; ".join(rej.reasons),
                    raw_data=json.dumps(raw, default=str),
                )
            )

    # -- reads ------------------------------------------------------------

    def get_customer(self, customer_id: int) -> Customer | None:
        return self.session.get(Customer, customer_id)

    def list_customers(
        self,
        search: str | None = None,
        country: str | None = None,
        plan: str | None = None,
        source: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Customer], int]:
        query = select(Customer)
        if search:
            like = f"%{escape_like(search.lower())}%"
            query = query.where(
                or_(
                    func.lower(Customer.first_name).like(like, escape="\\"),
                    func.lower(Customer.last_name).like(like, escape="\\"),
                    Customer.email.like(like, escape="\\"),  # stored lower-case
                )
            )
        if country:
            query = query.where(Customer.country == country.upper())
        if plan:
            # Normalise the input rather than wrapping the column in lower(),
            # which would prevent use of the index on `plan`.
            query = query.where(Customer.plan == RecordCleaner.clean_plan(plan))
        if source:
            query = query.where(Customer.sources.any(CustomerSource.source == source))

        total = self.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = self.session.scalars(query.order_by(Customer.id).limit(limit).offset(offset)).all()
        return list(rows), total

    def list_runs(self, limit: int = 20) -> list[PipelineRun]:
        return list(self.session.scalars(
            select(PipelineRun).order_by(PipelineRun.id.desc()).limit(limit)
        ).all())

    def get_run(self, run_id: int) -> PipelineRun | None:
        return self.session.get(PipelineRun, run_id)

    def last_finished_run(self, status: str | None = None) -> PipelineRun | None:
        query = select(PipelineRun).where(PipelineRun.status != "running")
        if status:
            query = query.where(PipelineRun.status == status)
        return self.session.scalar(query.order_by(PipelineRun.id.desc()).limit(1))

    def rejects_for_run(self, run_id: int) -> list[RejectedRow]:
        return list(self.session.scalars(
            select(RejectedRow).where(RejectedRow.run_id == run_id).order_by(RejectedRow.id)
        ).all())

    def stats(self) -> dict:
        by_country = self.session.execute(
            select(Customer.country, func.count()).group_by(Customer.country)
        ).all()
        by_plan = self.session.execute(
            select(Customer.plan, func.count()).group_by(Customer.plan)
        ).all()
        multi_source = self.session.scalar(
            select(func.count()).select_from(
                select(CustomerSource.customer_id)
                .group_by(CustomerSource.customer_id)
                .having(func.count(func.distinct(CustomerSource.source)) > 1)
                .subquery()
            )
        )
        return {
            "total_customers": self.session.scalar(select(func.count(Customer.id))) or 0,
            "in_multiple_sources": multi_source or 0,
            "by_country": {k or "unknown": v for k, v in by_country},
            "by_plan": {k or "none": v for k, v in by_plan},
        }
