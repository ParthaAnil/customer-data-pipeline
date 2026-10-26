"""Source-agnostic record types passed between pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class CustomerRecord:
    """A customer as seen by a single source system.

    Immutable: each stage returns a new record rather than mutating its input.
    `signup_date` is the raw string after extraction and a `date` after cleaning.
    """

    source: str
    source_id: str
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    city: str | None = None
    country: str | None = None
    signup_date: date | str | None = None
    plan: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def key(self) -> str:
        """Identifier unique across sources, e.g. ``crm:C001``."""
        return f"{self.source}:{self.source_id}"


@dataclass(frozen=True)
class RejectedRecord:
    record: CustomerRecord
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class GoldenCustomer:
    """The merged view of one customer across all sources."""

    first_name: str | None
    last_name: str | None
    email: str
    phone: str | None
    city: str | None
    country: str | None
    plan: str | None
    customer_since: date | None
    members: tuple[CustomerRecord, ...]
