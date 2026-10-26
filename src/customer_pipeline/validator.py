"""Record validation. Every rule runs, so a rejected record carries all its problems."""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Callable

from .models import CustomerRecord, RejectedRecord

logger = logging.getLogger(__name__)

EMAIL_PATTERN = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[A-Za-z]{2,}$")
PHONE_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")  # E.164
EARLIEST_SIGNUP = date(2000, 1, 1)
# Angle brackets enable stored XSS in any UI that renders these fields
# unescaped; control characters break CSV exports and log lines.
SUSPICIOUS_CHARS = re.compile(r"[<>\x00-\x1f\x7f]")

Rule = Callable[[CustomerRecord], "str | None"]


class RecordValidator:
    def __init__(self, today: date | None = None) -> None:
        # Injectable so tests don't depend on the real date.
        self.today = today or date.today()
        self.rules: list[Rule] = [
            self._has_name,
            self._text_fields_safe,
            self._email_present_and_valid,
            self._phone_valid_if_present,
            self._signup_date_sensible,
        ]

    def errors_for(self, record: CustomerRecord) -> list[str]:
        return [msg for rule in self.rules if (msg := rule(record))]

    def validate_all(
        self, records: list[CustomerRecord]
    ) -> tuple[list[CustomerRecord], list[RejectedRecord]]:
        valid, rejected = [], []
        for record in records:
            errors = self.errors_for(record)
            if errors:
                rejected.append(RejectedRecord(record=record, reasons=tuple(errors)))
                logger.warning("rejected %s: %s", record.key, "; ".join(errors))
            else:
                valid.append(record)
        logger.info("validation: %d valid, %d rejected", len(valid), len(rejected))
        return valid, rejected

    @staticmethod
    def _has_name(r: CustomerRecord) -> str | None:
        if not r.first_name and not r.last_name:
            return "missing name"
        return None

    @staticmethod
    def _text_fields_safe(r: CustomerRecord) -> str | None:
        bad = [name for name in ("first_name", "last_name", "city", "plan")
               if (value := getattr(r, name)) and SUSPICIOUS_CHARS.search(value)]
        if bad:
            return f"suspicious characters in {', '.join(bad)}"
        return None

    @staticmethod
    def _email_present_and_valid(r: CustomerRecord) -> str | None:
        if not r.email:
            return "missing email"
        if not EMAIL_PATTERN.match(r.email):
            return f"invalid email '{r.email}'"
        return None

    @staticmethod
    def _phone_valid_if_present(r: CustomerRecord) -> str | None:
        if r.phone and not PHONE_PATTERN.match(r.phone):
            return f"invalid phone '{r.phone}'"
        return None

    def _signup_date_sensible(self, r: CustomerRecord) -> str | None:
        if r.signup_date is None:
            raw = r.raw.get("signup_date") if r.raw else None
            return f"unreadable signup date '{raw}'" if raw else None
        if r.signup_date > self.today:
            return f"signup date {r.signup_date} is in the future"
        if r.signup_date < EARLIEST_SIGNUP:
            return f"signup date {r.signup_date} is before {EARLIEST_SIGNUP}"
        return None
