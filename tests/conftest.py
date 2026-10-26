"""Shared fixtures. Each test gets a fresh database (in-memory SQLite unless
TEST_DATABASE_URL points at PostgreSQL)."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from customer_pipeline.database import Base, init_db, make_engine, make_session_factory
from customer_pipeline.extractors import CsvCustomerExtractor, XmlCustomerExtractor
from customer_pipeline.models import CustomerRecord
from customer_pipeline.pipeline import CustomerPipeline
from customer_pipeline.validator import RecordValidator

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
FIXED_TODAY = date(2026, 10, 26)


@pytest.fixture
def crm_csv() -> Path:
    return DATA_DIR / "crm_customers.csv"


@pytest.fixture
def billing_xml() -> Path:
    return DATA_DIR / "billing_customers.xml"


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "sqlite://")


@pytest.fixture
def engine():
    eng = make_engine(TEST_DATABASE_URL)
    Base.metadata.drop_all(eng)
    init_db(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    return make_session_factory(engine)


@pytest.fixture
def pipeline(crm_csv, billing_xml, session_factory) -> CustomerPipeline:
    return CustomerPipeline(
        extractors=[CsvCustomerExtractor(crm_csv), XmlCustomerExtractor(billing_xml)],
        session_factory=session_factory,
        validator=RecordValidator(today=FIXED_TODAY),
    )


def make_record(**overrides) -> CustomerRecord:
    """A valid, already-clean record with selected fields overridden."""
    defaults = dict(
        source="crm", source_id="T1", first_name="Test", last_name="User",
        email="test.user@example.com", phone="+919000020001", city="Kochi",
        country="IN", signup_date=date(2024, 1, 1), plan=None,
    )
    defaults.update(overrides)
    return CustomerRecord(**defaults)
