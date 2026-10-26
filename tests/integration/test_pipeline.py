"""End-to-end: real sample files -> pipeline -> real (in-memory) SQL database."""

import json

import pytest
from sqlalchemy import func, select

from customer_pipeline.database import Customer, CustomerSource, PipelineRun, RejectedRow
from customer_pipeline.extractors import (
    CsvCustomerExtractor, ExtractionError, XmlCustomerExtractor,
)
from customer_pipeline.pipeline import CustomerPipeline


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_full_run_on_sample_data(pipeline, session_factory):
    report = pipeline.run()

    assert report.status == "success"
    assert report.per_source == {"crm": 12, "billing": 9}
    assert report.records_read == 21
    assert report.records_rejected == 4      # C004, C006, C008, B-1006
    assert report.records_valid == 17
    assert report.customers_created == 10    # 17 records -> 10 real people

    with session_factory() as s:
        assert count(s, Customer) == 10
        assert count(s, CustomerSource) == 17
        assert count(s, RejectedRow) == 4


def test_duplicates_are_merged_with_correct_survivorship(pipeline, session_factory):
    pipeline.run()
    with session_factory() as s:
        ananya = s.scalar(select(Customer).where(Customer.email == "ananya.menon@example.com"))
        assert {(x.source, x.source_id) for x in ananya.sources} == {
            ("crm", "C001"), ("crm", "C009"), ("billing", "B-1001"),
        }
        assert ananya.plan == "Pro"                        # from billing
        assert str(ananya.customer_since) == "2024-03-15"  # earliest of the three

        # Priya: different emails in CRM and billing, matched by phone only.
        priya = s.scalar(select(Customer).where(Customer.phone == "+919000010003"))
        assert priya.email == "priya.sharma@example.org"     # CRM email wins
        assert priya.first_name == "Priya"                 # not billing's "P."
        assert len(priya.sources) == 2

        # Rahul: CRM city wins over billing's "Trivandrum".
        rahul = s.scalar(select(Customer).where(Customer.email == "rahul.nair@example.net"))
        assert rahul.city == "Thiruvananthapuram"


def test_rejects_are_stored_with_reasons_and_raw_data(pipeline, session_factory):
    report = pipeline.run()
    with session_factory() as s:
        rejects = {
            r.source_id: r
            for r in s.scalars(select(RejectedRow).where(RejectedRow.run_id == report.run_id))
        }
    assert set(rejects) == {"C004", "C006", "C008", "B-1006"}
    assert "invalid email" in rejects["C004"].reasons
    assert "in the future" in rejects["C008"].reasons
    assert "missing name; missing email" == rejects["B-1006"].reasons
    assert json.loads(rejects["C004"].raw_data)["email"] == "arjun.das@example"


def test_running_twice_is_idempotent(pipeline, session_factory):
    first = pipeline.run()
    second = pipeline.run()

    assert first.customers_created == 10
    assert second.customers_created == 0
    assert second.customers_updated == 10
    with session_factory() as s:
        assert count(s, Customer) == 10
        assert count(s, CustomerSource) == 17
        assert count(s, PipelineRun) == 2


def test_new_source_record_attaches_to_existing_customer(
    crm_csv, tmp_path, session_factory, pipeline
):
    pipeline.run()

    # A later billing export has a brand-new account for an existing customer.
    later_xml = tmp_path / "billing_later.xml"
    later_xml.write_text(
        """<customers><customer id="B-2000"><name>Siddharth Rao</name>
             <contact><email>sid.rao@example.net</email></contact>
             <plan>Enterprise</plan></customer></customers>"""
    )
    CustomerPipeline(
        extractors=[CsvCustomerExtractor(crm_csv), XmlCustomerExtractor(later_xml)],
        session_factory=session_factory,
        validator=pipeline.validator,
    ).run()

    with session_factory() as s:
        sid = s.scalar(select(Customer).where(Customer.email == "sid.rao@example.net"))
        assert sid.plan == "Enterprise"
        assert ("billing", "B-2000") in {(x.source, x.source_id) for x in sid.sources}


def test_bad_input_file_fails_run_and_leaves_no_partial_data(
    crm_csv, tmp_path, session_factory, pipeline
):
    broken = tmp_path / "broken.xml"
    broken.write_text("<customers><customer id='1'>")
    bad_pipeline = CustomerPipeline(
        extractors=[CsvCustomerExtractor(crm_csv), XmlCustomerExtractor(broken)],
        session_factory=session_factory,
        validator=pipeline.validator,
    )

    with pytest.raises(ExtractionError):
        bad_pipeline.run()

    with session_factory() as s:
        assert count(s, Customer) == 0  # CRM rows were NOT half-loaded
        run = s.scalar(select(PipelineRun))
        assert run.status == "failed"
        assert "invalid XML" in run.error
        assert run.finished_at is not None
