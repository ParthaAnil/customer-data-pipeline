"""Repository behaviour that needs a real database: N+1, locking, escaping."""

from datetime import date, timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.orm.exc import StaleDataError

from customer_pipeline.database import Customer, PipelineRun, utcnow
from customer_pipeline.models import GoldenCustomer
from customer_pipeline.repository import CustomerRepository, RunAlreadyInProgress, escape_like
from tests.conftest import TEST_DATABASE_URL, make_record


def golden(i: int) -> GoldenCustomer:
    member = make_record(source="crm", source_id=f"C{i}", email=f"user{i}@example.com")
    return GoldenCustomer(
        first_name=f"User{i}", last_name="Test", email=f"user{i}@example.com", phone=None,
        city="Kochi", country="IN", plan="Pro", customer_since=date(2024, 1, 1),
        members=(member,),
    )


class QueryCounter:
    """Counts SELECT statements sent to the database."""

    def __init__(self, engine):
        self.engine, self.selects = engine, 0

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._count)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._count)

    def _count(self, _conn, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("SELECT"):
            self.selects += 1


def selects_to_upsert(engine, session_factory, n: int) -> tuple[int, int]:
    """SELECTs for inserting n new customers, then for updating the same n."""
    batch = [golden(i) for i in range(n)]
    counts = []
    for _ in range(2):  # first pass inserts, second pass updates
        with session_factory() as s, QueryCounter(engine) as qc:
            CustomerRepository(s).upsert_customers(batch)
            s.commit()
        counts.append(qc.selects)
    return counts[0], counts[1]


def test_no_n_plus_1_select_count_does_not_grow_with_customers(engine, session_factory):
    small = selects_to_upsert(engine, session_factory, 5)
    # fresh database for the larger batch
    with session_factory() as s:
        s.query(Customer).delete()
        s.commit()
    large = selects_to_upsert(engine, session_factory, 200)
    assert small == large            # same number of queries for 5 or 200 customers
    assert large[1] <= 5             # and only a handful in total


def test_unchanged_rows_are_not_rewritten(engine, session_factory):
    with session_factory() as s:
        CustomerRepository(s).upsert_customers([golden(1)])
        s.commit()
    with session_factory() as s:
        CustomerRepository(s).upsert_customers([golden(1)])
        s.commit()
        assert s.query(Customer).one().version == 1  # nothing changed -> no version bump


def test_optimistic_lock_blocks_lost_update(session_factory):
    with session_factory() as s:
        CustomerRepository(s).upsert_customers([golden(1)])
        s.commit()

    first, second = session_factory(), session_factory()
    a = first.query(Customer).one()     # both read version 1
    b = second.query(Customer).one()
    a.city = "Thrissur"
    first.commit()                      # version -> 2
    b.city = "Kannur"
    with pytest.raises(StaleDataError):
        second.commit()                 # still thinks it's version 1: refused
    first.close()
    second.close()


def test_run_lock_allows_only_one_running_run(session_factory):
    with session_factory() as s:
        CustomerRepository(s).start_run()
        s.commit()
    with session_factory() as s, pytest.raises(RunAlreadyInProgress):
        CustomerRepository(s).start_run()


def test_finished_runs_do_not_hold_the_lock(session_factory):
    with session_factory() as s:
        repo = CustomerRepository(s)
        run = repo.start_run()
        repo.finish_run(run, "success")
        s.commit()
    with session_factory() as s:
        CustomerRepository(s).start_run()  # no error
        s.commit()


def test_expire_stale_runs_only_touches_old_running_rows(session_factory):
    with session_factory() as s:
        s.add_all([
            PipelineRun(status="running", started_at=utcnow() - timedelta(hours=1)),
        ])
        s.commit()
        assert CustomerRepository(s).expire_stale_runs(timedelta(minutes=30)) == 1
        s.commit()
        s.add(PipelineRun(status="running"))  # fresh
        s.commit()
        assert CustomerRepository(s).expire_stale_runs(timedelta(minutes=30)) == 0


@pytest.mark.parametrize("raw, escaped", [
    ("50%", r"50\%"), ("a_b", r"a\_b"), ("back\\slash", "back\\\\slash"), ("plain", "plain"),
])
def test_escape_like(raw, escaped):
    assert escape_like(raw) == escaped


@pytest.mark.skipif(not TEST_DATABASE_URL.startswith("postgresql"),
                    reason="needs a real multi-connection database (CI runs it on PostgreSQL)")
def test_run_lock_under_real_concurrency(session_factory):
    """20 threads try to start a run at the same instant: exactly one may win."""
    import threading

    barrier = threading.Barrier(20)
    won, refused = [], []

    def attempt():
        with session_factory() as s:
            barrier.wait()  # release all threads together
            try:
                CustomerRepository(s).start_run()
                s.commit()
                won.append(1)
            except RunAlreadyInProgress:
                refused.append(1)

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert (len(won), len(refused)) == (1, 19)
