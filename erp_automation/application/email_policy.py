"""Shared desktop policy for the optional local e-mail preview stage."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def email_preview_enabled(configuration: Mapping[str, Any]) -> bool:
    """Return the product-wide mail switch for this release.

    Mail delivery has not been integrated, so both local preview generation
    and its receiver-email backfill are deliberately disabled regardless of
    stale values in an older encrypted configuration.  The argument remains
    in the interface so a future mail-enabled release can restore the policy
    without changing callers.
    """

    del configuration
    return False


__all__ = ["email_preview_enabled"]
