from datetime import date

import pytest

from customer_pipeline.validator import RecordValidator
from tests.conftest import FIXED_TODAY, make_record


@pytest.fixture
def validator():
    return RecordValidator(today=FIXED_TODAY)


def test_valid_record_has_no_errors(validator):
    assert validator.errors_for(make_record()) == []


@pytest.mark.parametrize(
    "overrides, expected_error",
    [
        ({"first_name": None, "last_name": None}, "missing name"),
        ({"email": None}, "missing email"),
        ({"email": "arjun.das@example"}, "invalid email"),
        ({"email": "two@@example.com"}, "invalid email"),
        ({"phone": "+0123"}, "invalid phone"),
        ({"signup_date": date(2027, 1, 1)}, "in the future"),
        ({"signup_date": date(1990, 1, 1)}, "before 2000"),
    ],
)
def test_each_rule_reports_its_problem(validator, overrides, expected_error):
    errors = validator.errors_for(make_record(**overrides))
    assert len(errors) == 1
    assert expected_error in errors[0]


def test_one_name_part_is_enough(validator):
    assert validator.errors_for(make_record(last_name=None)) == []


def test_phone_is_optional(validator):
    assert validator.errors_for(make_record(phone=None)) == []


def test_all_problems_are_reported_together(validator):
    record = make_record(first_name=None, last_name=None, email=None, phone="+0")
    assert len(validator.errors_for(record)) == 3


def test_unreadable_date_is_reported_using_the_raw_value(validator):
    record = make_record(signup_date=None, raw={"signup_date": "31-31-2024"})
    assert validator.errors_for(record) == ["unreadable signup date '31-31-2024'"]


def test_missing_date_is_allowed(validator):
    assert validator.errors_for(make_record(signup_date=None)) == []


def test_validate_all_splits_valid_and_rejected(validator):
    good = make_record(source_id="1")
    bad = make_record(source_id="2", email=None)
    valid, rejected = validator.validate_all([good, bad])
    assert valid == [good]
    assert len(rejected) == 1
    assert rejected[0].record == bad
    assert rejected[0].reasons == ("missing email",)


@pytest.mark.parametrize(
    "overrides",
    [
        {"first_name": "<script>alert(1)</script>"},
        {"city": "Kochi<img src=x onerror=alert(1)>"},
        {"last_name": "Nair\nFAKE LOG LINE"},
        {"email": "<script>@example.com"},
    ],
)
def test_html_and_control_characters_are_rejected(validator, overrides):
    errors = validator.errors_for(make_record(**overrides))
    assert errors and ("suspicious characters" in errors[0] or "invalid email" in errors[0])


def test_normal_punctuation_in_names_is_fine(validator):
    record = make_record(first_name="Mary-Ann", last_name="D'Souza", city="St. Thomas Mount")
    assert validator.errors_for(record) == []
