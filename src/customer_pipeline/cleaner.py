"""Field normalisation.

The cleaner only reformats; it never rejects. Values it cannot interpret
become None and are reported by the validator.

    ' Rahul.Nair@Example.net '  -> 'rahul.nair@example.net'
    '+91 90000 10002'           -> '+919000010002'      (E.164)
    'india' / 'IND'             -> 'IN'                  (ISO 3166-1 alpha-2)
    '15/04/2024'                -> date(2024, 4, 15)
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import date, datetime

from .models import CustomerRecord

logger = logging.getLogger(__name__)

COUNTRY_ALIASES = {
    "in": "IN", "ind": "IN", "india": "IN", "bharat": "IN",
    "us": "US", "usa": "US", "united states": "US", "united states of america": "US",
    "uk": "GB", "gb": "GB", "united kingdom": "GB", "great britain": "GB",
    "ae": "AE", "uae": "AE", "united arab emirates": "AE",
}

DIALLING_CODES = {"IN": "91", "US": "1", "GB": "44", "AE": "971"}
DEFAULT_COUNTRY = "IN"

# Day-first formats precede month-first ones: both sources are Indian systems.
DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
)

PLAN_NAMES = {"basic": "Basic", "pro": "Pro", "enterprise": "Enterprise"}


class RecordCleaner:
    def clean(self, record: CustomerRecord) -> CustomerRecord:
        country = self.clean_country(record.country)
        return replace(
            record,
            first_name=self.clean_name(record.first_name),
            last_name=self.clean_name(record.last_name),
            email=self.clean_email(record.email),
            phone=self.clean_phone(record.phone, country),
            city=self.clean_name(record.city),
            country=country,
            signup_date=self.clean_date(record.signup_date),
            plan=self.clean_plan(record.plan),
        )

    def clean_all(self, records: list[CustomerRecord]) -> list[CustomerRecord]:
        cleaned = [self.clean(r) for r in records]
        logger.info("cleaned %d records", len(cleaned))
        return cleaned

    @staticmethod
    def _blank_to_none(value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        return value or None

    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        value = cls._blank_to_none(value)
        if value is None:
            return None
        # Re-case only all-lower or all-upper input so "McDonald" survives.
        if value.islower() or value.isupper():
            value = value.title()
        return value

    @classmethod
    def clean_email(cls, value: str | None) -> str | None:
        value = cls._blank_to_none(value)
        return value.lower() if value else None

    @classmethod
    def clean_country(cls, value: str | None) -> str | None:
        value = cls._blank_to_none(value)
        if value is None:
            return None
        return COUNTRY_ALIASES.get(value.lower(), value.upper())

    @classmethod
    def clean_phone(cls, value: str | None, country: str | None = None) -> str | None:
        """Normalise to E.164, using the record's country when no '+code' is given."""
        value = cls._blank_to_none(value)
        if value is None:
            return None

        has_plus = value.startswith("+")
        digits = re.sub(r"\D", "", value)
        if not digits:
            return None
        if has_plus:
            return f"+{digits}"

        code = DIALLING_CODES.get(country or DEFAULT_COUNTRY, DIALLING_CODES[DEFAULT_COUNTRY])
        digits = digits.lstrip("0")  # national trunk prefix
        if digits.startswith(code) and len(digits) > 10:
            return f"+{digits}"
        return f"+{code}{digits}"

    @staticmethod
    def clean_date(value: date | str | None) -> date | None:
        if value is None or isinstance(value, date):
            return value
        value = value.strip()
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
        logger.debug("could not parse date %r", value)
        return None

    @classmethod
    def clean_plan(cls, value: str | None) -> str | None:
        value = cls._blank_to_none(value)
        if value is None:
            return None
        return PLAN_NAMES.get(value.lower(), value)
