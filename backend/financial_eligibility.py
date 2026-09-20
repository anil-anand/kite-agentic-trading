"""Shared rules for outcomes that are safe to use as financial evidence."""

from __future__ import annotations

import math
import sys
from typing import Any, Iterable, Mapping

VERIFIED_FINANCIAL_QUALITY = "RECONCILED"
_REPAIRED_OR_LEGACY_PROVENANCE = {
    "legacy_zero_price_placeholder",
    "pending_broker_fill_reconciliation",
    "unknown",
}


def verified_outcome(row: Mapping[str, Any]) -> bool:
    """Return whether a closed row has a verified, usable financial outcome.

    ``ESTIMATED`` rows, unavailable/legacy placeholders and repaired rows that
    still carry an unresolved provenance are deliberately excluded.  A repair
    that has subsequently been reconciled may be used when its final quality is
    ``RECONCILED`` and its prices/net result are valid.
    """

    if row.get("status") != "CLOSED":
        return False
    if "financial_quality" in row:
        if row.get("financial_quality") != VERIFIED_FINANCIAL_QUALITY:
            return False
        if row.get("financial_provenance") in _REPAIRED_OR_LEGACY_PROVENANCE:
            return False

    try:
        entry_price = float(row.get("entry_price"))
        exit_price = float(row.get("exit_price"))
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(entry_price) and math.isfinite(exit_price)):
        return False
    if entry_price <= 0 or exit_price <= 0:
        return False

    net_pnl = row.get("net_pnl", row.get("pnl"))
    try:
        net_pnl = float(net_pnl)
    except (TypeError, ValueError):
        return False
    return math.isfinite(net_pnl)


def verified_outcome_sql(columns: Iterable[str]) -> str:
    """Return the same eligibility predicate for a SQLite table.

    Old read-only fixtures predate ``financial_quality``.  Positive prices and
    a finite P&L are the compatibility rule for those rows; modern tables must
    explicitly say that the result was reconciled.
    """

    columns = set(columns)
    parts = ["status = 'CLOSED'", "entry_price > 0", "exit_price > 0"]
    numeric_fields = ["entry_price", "exit_price"]
    if "financial_quality" in columns:
        parts.append("financial_quality = 'RECONCILED'")
        if "financial_provenance" in columns:
            parts.append(
                "COALESCE(financial_provenance, '') NOT IN "
                "('legacy_zero_price_placeholder', "
                "'pending_broker_fill_reconciliation', 'unknown')"
            )
    if "net_pnl" in columns:
        numeric_fields.append("net_pnl")
    elif "pnl" in columns:
        numeric_fields.append("pnl")
    else:
        parts.append("0")
    for field in numeric_fields:
        parts.append(f"typeof({field}) IN ('integer', 'real')")
        parts.append(f"{field} BETWEEN {-sys.float_info.max} AND {sys.float_info.max}")
    return " AND ".join(parts)
