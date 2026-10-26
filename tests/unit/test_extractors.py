import pytest

from customer_pipeline.extractors import (
    CsvCustomerExtractor, ExtractionError, XmlCustomerExtractor,
)

CSV_HEADER = "customer_id,first_name,last_name,email,phone,city,country,signup_date\n"


def test_csv_reads_rows_into_records(tmp_path):
    f = tmp_path / "crm.csv"
    f.write_text(CSV_HEADER + "C1,Asha,K,asha@example.com,9000020001,Kochi,IN,2024-01-01\n")
    [record] = CsvCustomerExtractor(f).extract()
    assert record.source == "crm"
    assert record.source_id == "C1"
    assert record.email == "asha@example.com"
    assert record.raw["signup_date"] == "2024-01-01"


def test_csv_handles_excel_bom(tmp_path):
    f = tmp_path / "crm.csv"
    f.write_bytes(("\ufeff" + CSV_HEADER + "C1,A,B,a@example.com,,,,\n").encode("utf-8"))
    assert CsvCustomerExtractor(f).extract()[0].source_id == "C1"


def test_csv_skips_rows_without_id(tmp_path):
    f = tmp_path / "crm.csv"
    f.write_text(CSV_HEADER + ",A,B,a@example.com,,,,\nC2,C,D,c@example.com,,,,\n")
    assert [r.source_id for r in CsvCustomerExtractor(f).extract()] == ["C2"]


def test_csv_missing_columns_raises(tmp_path):
    f = tmp_path / "crm.csv"
    f.write_text("id,name\n1,x\n")
    with pytest.raises(ExtractionError, match="missing columns"):
        CsvCustomerExtractor(f).extract()


def test_missing_file_raises(tmp_path):
    with pytest.raises(ExtractionError, match="file not found"):
        CsvCustomerExtractor(tmp_path / "nope.csv").extract()


def test_xml_reads_nested_fields_and_splits_name(tmp_path):
    f = tmp_path / "billing.xml"
    f.write_text(
        """<customers><customer id="B-1">
             <name>Mary Ann Thomas</name>
             <contact><email>m@example.com</email><phone>9000020001</phone></contact>
             <address><city>Kochi</city><country>IN</country></address>
             <account_created>2024-01-01T10:00:00</account_created><plan>Pro</plan>
           </customer></customers>"""
    )
    [r] = XmlCustomerExtractor(f).extract()
    assert (r.source, r.source_id) == ("billing", "B-1")
    assert (r.first_name, r.last_name) == ("Mary Ann", "Thomas")
    assert (r.email, r.phone, r.city, r.plan) == ("m@example.com", "9000020001", "Kochi", "Pro")


def test_xml_missing_optional_tags_become_none(tmp_path):
    f = tmp_path / "billing.xml"
    f.write_text('<customers><customer id="B-2"><name>Solo</name></customer></customers>')
    [r] = XmlCustomerExtractor(f).extract()
    assert (r.first_name, r.last_name, r.email, r.phone) == ("Solo", None, None, None)


def test_xml_skips_customer_without_id(tmp_path):
    f = tmp_path / "billing.xml"
    f.write_text("<customers><customer><name>X</name></customer></customers>")
    assert XmlCustomerExtractor(f).extract() == []


def test_xml_broken_file_raises(tmp_path):
    f = tmp_path / "billing.xml"
    f.write_text("<customers><customer id='1'>")
    with pytest.raises(ExtractionError, match="invalid XML"):
        XmlCustomerExtractor(f).extract()


def test_xml_wrong_root_raises(tmp_path):
    f = tmp_path / "billing.xml"
    f.write_text("<orders/>")
    with pytest.raises(ExtractionError, match="expected <customers>"):
        XmlCustomerExtractor(f).extract()


def test_xml_entity_attack_is_blocked(tmp_path):
    """defusedxml refuses XML that defines entities (XXE / billion-laughs)."""
    f = tmp_path / "evil.xml"
    f.write_text(
        '<?xml version="1.0"?><!DOCTYPE c [<!ENTITY x "boom">]>'
        "<customers><customer id='1'><name>&x;</name></customer></customers>"
    )
    with pytest.raises(ExtractionError):
        XmlCustomerExtractor(f).extract()
