"""API response models, kept separate from the ORM so the contract changes only on purpose."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class SourceLinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source: str
    source_id: str


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    first_name: str | None
    last_name: str | None
    email: str
    phone: str | None
    city: str | None
    country: str | None
    plan: str | None
    customer_since: date | None


class CustomerDetailOut(CustomerOut):
    version: int
    sources: list[SourceLinkOut]
    created_at: datetime
    updated_at: datetime


class CustomerPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[CustomerOut]


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    records_read: int
    records_valid: int
    records_rejected: int
    customers_created: int
    customers_updated: int
    error: str | None


class RejectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source: str
    source_id: str
    reasons: str
    raw_data: str


class StatsOut(BaseModel):
    total_customers: int
    in_multiple_sources: int
    by_country: dict[str, int]
    by_plan: dict[str, int]
