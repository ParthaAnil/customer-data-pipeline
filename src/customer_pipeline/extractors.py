"""Source readers that map each export format onto `CustomerRecord`.

Adding a source (JSON, SOAP, ...) means adding one `BaseExtractor` subclass;
downstream stages are format-agnostic.
"""

from __future__ import annotations

import csv
import logging
from abc import ABC, abstractmethod
from pathlib import Path
# Only the Element type and the serialiser come from the stdlib; all parsing
# goes through defusedxml below.
from xml.etree.ElementTree import Element, tostring  # nosec B405

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from .models import CustomerRecord

logger = logging.getLogger(__name__)


class ExtractionError(Exception):
    """The source file is missing or unreadable as a whole."""


class BaseExtractor(ABC):
    source_name: str = "unknown"

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def extract(self) -> list[CustomerRecord]:
        if not self.path.exists():
            raise ExtractionError(f"{self.source_name}: file not found: {self.path}")
        records = self._read()
        logger.info("%s: read %d records from %s", self.source_name, len(records), self.path.name)
        return records

    @abstractmethod
    def _read(self) -> list[CustomerRecord]: ...


class CsvCustomerExtractor(BaseExtractor):
    """CRM export: one customer per row."""

    source_name = "crm"
    REQUIRED_COLUMNS = {
        "customer_id", "first_name", "last_name", "email",
        "phone", "city", "country", "signup_date",
    }

    def _read(self) -> list[CustomerRecord]:
        # utf-8-sig strips the BOM that Excel writes at the start of CSV files.
        with self.path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            missing = self.REQUIRED_COLUMNS - set(reader.fieldnames or [])
            if missing:
                raise ExtractionError(f"crm: CSV is missing columns: {sorted(missing)}")

            records = []
            for line_no, row in enumerate(reader, start=2):
                if not (row.get("customer_id") or "").strip():
                    logger.warning("crm: line %d has no customer_id, skipped", line_no)
                    continue
                records.append(
                    CustomerRecord(
                        source=self.source_name,
                        source_id=row["customer_id"].strip(),
                        first_name=row["first_name"],
                        last_name=row["last_name"],
                        email=row["email"],
                        phone=row["phone"],
                        city=row["city"],
                        country=row["country"],
                        signup_date=row["signup_date"],
                        raw=dict(row),
                    )
                )
        return records


class XmlCustomerExtractor(BaseExtractor):
    """Billing export: ``<customers><customer id="...">...</customer></customers>``."""

    source_name = "billing"

    def _read(self) -> list[CustomerRecord]:
        try:
            # defusedxml rejects entity declarations (XXE, billion laughs).
            root = SafeET.parse(self.path).getroot()
        except (SafeET.ParseError, DefusedXmlException) as exc:
            raise ExtractionError(f"billing: invalid XML in {self.path.name}: {exc}") from exc

        if root.tag != "customers":
            raise ExtractionError(f"billing: expected <customers> root, got <{root.tag}>")

        records = []
        for node in root.findall("customer"):
            source_id = (node.get("id") or "").strip()
            if not source_id:
                logger.warning("billing: <customer> without id attribute, skipped")
                continue
            first, last = self._split_name(self._text(node, "name"))
            signup = self._text(node, "account_created")
            records.append(
                CustomerRecord(
                    source=self.source_name,
                    source_id=source_id,
                    first_name=first,
                    last_name=last,
                    email=self._text(node, "contact/email"),
                    phone=self._text(node, "contact/phone"),
                    city=self._text(node, "address/city"),
                    country=self._text(node, "address/country"),
                    signup_date=signup,
                    plan=self._text(node, "plan"),
                    raw={
                        "id": source_id,
                        "signup_date": signup,
                        "xml": tostring(node, encoding="unicode").strip(),
                    },
                )
            )
        return records

    @staticmethod
    def _text(node: Element, path: str) -> str | None:
        child = node.find(path)
        return child.text if child is not None and child.text else None

    @staticmethod
    def _split_name(full_name: str | None) -> tuple[str | None, str | None]:
        """Treat the last word as the surname: 'Mary Ann Thomas' -> ('Mary Ann', 'Thomas')."""
        if not full_name or not full_name.strip():
            return None, None
        parts = full_name.split()
        if len(parts) == 1:
            return parts[0], None
        return " ".join(parts[:-1]), parts[-1]
