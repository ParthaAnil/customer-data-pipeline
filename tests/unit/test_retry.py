import pytest
from sqlalchemy.exc import OperationalError

from customer_pipeline.database import make_engine, wait_for_db
from customer_pipeline.retry import backoff_delays, retry


def test_backoff_doubles_and_is_capped():
    assert backoff_delays(attempts=6, base=0.5, maximum=4) == [0.5, 1, 2, 4, 4]


def test_retry_succeeds_after_transient_failures():
    calls, waits = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("not yet")
        return "ok"

    result = retry(flaky, attempts=5, base_delay=1, sleep=waits.append, jitter=lambda d: 0)
    assert result == "ok"
    assert len(calls) == 3
    assert waits == [1, 2]  # exponential: 1 s then 2 s


def test_retry_gives_up_and_reraises():
    waits = []
    with pytest.raises(ConnectionError):
        retry(lambda: (_ for _ in ()).throw(ConnectionError("down")),
              attempts=3, base_delay=1, sleep=waits.append, jitter=lambda d: 0)
    assert waits == [1, 2]


def test_retry_does_not_retry_unexpected_errors():
    calls = []

    def bug():
        calls.append(1)
        raise ValueError("a bug, not a blip")

    with pytest.raises(ValueError):
        retry(bug, retry_on=(ConnectionError,), sleep=lambda _: None)
    assert len(calls) == 1


def test_jitter_adds_up_to_half_the_delay():
    waits = []
    with pytest.raises(ConnectionError):
        retry(lambda: (_ for _ in ()).throw(ConnectionError()), attempts=4, base_delay=1,
              sleep=waits.append)
    for wait, base in zip(waits, [1, 2, 4]):
        assert base <= wait <= base * 1.5


def test_wait_for_db_retries_until_unreachable_db_gives_up(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/no/such/dir/x.db")  # can't be opened
    waits = []
    with pytest.raises(OperationalError):
        wait_for_db(engine, attempts=3, sleep=waits.append)
    assert len(waits) == 2
