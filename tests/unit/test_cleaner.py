from datetime import date

import pytest

from customer_pipeline.cleaner import RecordCleaner
from customer_pipeline.models import CustomerRecord


@pytest.mark.parametrize(
    "raw, expected",
    [
        (" Rahul.Nair@Example.net ", "rahul.nair@example.net"),
        ("ANANYA.MENON@EXAMPLE.COM", "ananya.menon@example.com"),
        ("   ", None),
        (None, None),
    ],
)
def test_clean_email(raw, expected):
    assert RecordCleaner.clean_email(raw) == expected


@pytest.mark.parametrize(
    "raw, country, expected",
    [
        ("9000010001", "IN", "+919000010001"),        # plain 10-digit mobile
        ("+91 90000 10002", "IN", "+919000010002"),   # spaces removed
        ("090000-10003", "IN", "+919000010003"),      # trunk 0 and dash removed
        ("919000010001", "IN", "+919000010001"),      # code present, '+' missing
        ("(415) 555-0199", "US", "+14155550199"),     # US number, US code added
        ("9000010001", None, "+919000010001"),        # unknown country -> default IN
        ("", "IN", None),
        ("n/a", "IN", None),                          # no digits at all
    ],
)
def test_clean_phone(raw, country, expected):
    assert RecordCleaner.clean_phone(raw, country) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [("India", "IN"), ("india", "IN"), ("IND", "IN"), ("USA", "US"),
     ("United Kingdom", "GB"), ("fr", "FR"), (None, None)],
)
def test_clean_country(raw, expected):
    assert RecordCleaner.clean_country(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2024-03-15", date(2024, 3, 15)),
        ("15/04/2024", date(2024, 4, 15)),            # day first
        ("2024/08/19", date(2024, 8, 19)),
        ("20-10-2024", date(2024, 10, 20)),
        ("2024-03-15T10:30:00", date(2024, 3, 15)),   # XML timestamp -> date
        ("31-31-2024", None),                         # impossible date
        ("yesterday", None),
        (None, None),
    ],
)
def test_clean_date(raw, expected):
    assert RecordCleaner.clean_date(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [("rahul", "Rahul"), ("ANANYA", "Ananya"), ("McDonald", "McDonald"),
     ("  priya   ann ", "Priya Ann"), ("", None)],
)
def test_clean_name_only_recases_all_lower_or_upper(raw, expected):
    assert RecordCleaner.clean_name(raw) == expected


def test_clean_whole_record_uses_country_for_phone():
    raw = CustomerRecord(
        source="billing", source_id="B-1", first_name="tom", last_name="GEORGE",
        email=" Tom@Example.com ", phone="415 555 0199", city="san francisco",
        country="usa", signup_date="2024-08-01T17:45:00", plan="pro",
    )
    clean = RecordCleaner().clean(raw)
    assert clean.first_name == "Tom"
    assert clean.last_name == "George"
    assert clean.email == "tom@example.com"
    assert clean.country == "US"
    assert clean.phone == "+14155550199"
    assert clean.city == "San Francisco"
    assert clean.signup_date == date(2024, 8, 1)
    assert clean.plan == "Pro"


def test_clean_does_not_modify_the_original_record():
    raw = CustomerRecord(source="crm", source_id="C1", email="A@B.COM")
    RecordCleaner().clean(raw)
    assert raw.email == "A@B.COM"
