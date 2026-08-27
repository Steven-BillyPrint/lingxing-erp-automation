"""Stable application contracts for authenticated Lingxing order details.

The application layer deliberately knows neither Lingxing's private URLs nor
its raw JSON schema.  Those details belong to the integration adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


@dataclass(frozen=True)
class ContactSnapshot:
    """Normalized contact values observed in one authoritative detail read."""

    phone: str | None = None
    email: str | None = None


@dataclass(frozen=True)
class ContactPatch:
    """Only non-``None`` fields are allowed to change."""

    phone: str | None = None
    email: str | None = None

    @property
    def empty(self) -> bool:
        return self.phone is None and self.email is None


@dataclass(frozen=True)
class InternalOrderDetail:
    """Business-facing projection of one verified internal order detail."""

    system_order_no: str
    platform_order_nos: tuple[str, ...]
    recipient_name: str | None
    address_line1: str | None
    address_line2: str | None
    address_line3: str | None
    city: str | None
    state_or_region: str | None
    country_code: str | None
    country_name: str | None
    postal_code: str | None
    shipping_address_text: str
    contact: ContactSnapshot
    status: str
    revision: str
    request_id: str | None = None


class ContactWriteStatus(str, Enum):
    """Closed set of write outcomes understood by the workflow."""

    ALREADY_CURRENT = "already_current"
    CONFIRMED_APPLIED = "confirmed_applied"
    CONFLICT = "conflict"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class ContactWriteOutcome:
    """Result of at most one internal edit request followed by GET polling."""

    status: ContactWriteStatus
    attempted: bool
    before: ContactSnapshot
    after: ContactSnapshot | None
    message: str
    request_id: str | None = None
    attempts: int = 0
    waited_seconds: float = 0.0

    @property
    def completed(self) -> bool:
        return self.status in {
            ContactWriteStatus.ALREADY_CURRENT,
            ContactWriteStatus.CONFIRMED_APPLIED,
        }


class InternalOrderOperations(Protocol):
    """Port used by order workflows; implemented by an authenticated adapter."""

    async def get_order_detail(
        self,
        system_order_no: str,
        expected_platform_order_no: str,
    ) -> InternalOrderDetail: ...

    async def update_contacts(
        self,
        system_order_no: str,
        expected_platform_order_no: str,
        patch: ContactPatch,
        *,
        expected_revision: str,
    ) -> ContactWriteOutcome: ...


__all__ = [
    "ContactPatch",
    "ContactSnapshot",
    "ContactWriteOutcome",
    "ContactWriteStatus",
    "InternalOrderDetail",
    "InternalOrderOperations",
]
