"""Duplicate resolution: match records to customers, then build one golden record each.

Matching: records sharing an email or a phone belong to the same customer.
Matching is transitive (A~B by email and B~C by phone puts A, B, C together),
so groups are computed with union-find.

Survivorship: each field takes the first non-empty value in its source
priority order. Names and contact details trust the CRM; the plan trusts
billing, which is the system that charges it. `customer_since` is the
earliest signup date seen.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from .models import CustomerRecord, GoldenCustomer

logger = logging.getLogger(__name__)

FIELD_PRIORITY: dict[str, tuple[str, ...]] = {
    "first_name": ("crm", "billing"),
    "last_name": ("crm", "billing"),
    "email": ("crm", "billing"),
    "phone": ("crm", "billing"),
    "city": ("crm", "billing"),
    "country": ("crm", "billing"),
    "plan": ("billing", "crm"),
}


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]  # path halving
            item = self.parent[item]
        return item

    def union(self, a: str, b: str) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self.parent[root_b] = root_a


class CustomerMerger:
    def __init__(self, field_priority: dict[str, tuple[str, ...]] | None = None) -> None:
        self.field_priority = field_priority or FIELD_PRIORITY

    def merge(self, records: list[CustomerRecord]) -> list[GoldenCustomer]:
        golden = [self._build_golden(members) for members in self._group(records)]
        logger.info("merged %d records into %d customers", len(records), len(golden))
        return golden

    def _group(self, records: list[CustomerRecord]) -> list[list[CustomerRecord]]:
        uf = UnionFind()
        first_owner: dict[str, str] = {}  # match key -> first record key seen with it

        for record in records:
            uf.find(record.key)
            for match_key in self._match_keys(record):
                if match_key in first_owner:
                    uf.union(first_owner[match_key], record.key)
                    logger.debug("match on %s: %s + %s",
                                 match_key, first_owner[match_key], record.key)
                else:
                    first_owner[match_key] = record.key

        groups: dict[str, list[CustomerRecord]] = defaultdict(list)
        for record in records:
            groups[uf.find(record.key)].append(record)
        return list(groups.values())

    @staticmethod
    def _match_keys(record: CustomerRecord) -> list[str]:
        keys = []
        if record.email:
            keys.append(f"email:{record.email}")
        if record.phone:
            keys.append(f"phone:{record.phone}")
        return keys

    def _build_golden(self, members: list[CustomerRecord]) -> GoldenCustomer:
        members = sorted(members, key=lambda r: (r.source, r.source_id))  # deterministic output
        dates = [r.signup_date for r in members if r.signup_date]
        return GoldenCustomer(
            first_name=self._pick(members, "first_name"),
            last_name=self._pick(members, "last_name"),
            email=self._pick(members, "email"),
            phone=self._pick(members, "phone"),
            city=self._pick(members, "city"),
            country=self._pick(members, "country"),
            plan=self._pick(members, "plan"),
            customer_since=min(dates) if dates else None,
            members=tuple(members),
        )

    def _pick(self, members: list[CustomerRecord], field_name: str):
        priority = self.field_priority.get(field_name, ())
        rank = {source: i for i, source in enumerate(priority)}
        for record in sorted(members, key=lambda r: rank.get(r.source, len(priority))):
            value = getattr(record, field_name)
            if value:
                return value
        return None
