"""WP4 D1 — ID and price parser for the frozen Office_Products dataset.

Satisfies the proposal's identity/price contract (WP4-proposal-source.md,
"Product identity, prices and strict API integration"):

  "Prefer ASIN when available ... Prices are a typed (amount, currency) pair
  with parser provenance. Missing/failed parse is unknown, never zero."

Identity: Amazon Reviews 2023's own `parent_asin` field already IS the ASIN,
so this dataset satisfies "prefer ASIN when available" by construction — no
fallback to a stable-provider-ID or (brand, model) identity is needed here,
and titles are never used for matching (`parse_item_id` doesn't look at title
at all, only validates the ASIN's format).

Price: reuses `parse_price()` from the live system's
`langgraph_metarec.product_constraints` rather than reimplementing number
parsing — this dataset's `price` field is already a plain float or null (not
a string like SerpApi's `"$38.00"`), which `parse_price()` already handles
via its `isinstance(value, (int, float))` branch. This module only adds the
currency tag and provenance that `parse_price()` itself doesn't track.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from langgraph_metarec.product_constraints import parse_price as _parse_price_value  # noqa: E402

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")

# Recorded decision (Project Plan, Section 6, "Currency"): USD, since the
# dataset itself is priced in USD. No exchange-table conversion is attempted.
DATASET_CURRENCY = "USD"


@dataclass(frozen=True)
class ParsedPrice:
    amount: Optional[float]
    currency: Optional[str]  # None exactly when amount is None
    provenance: str

    def __post_init__(self) -> None:
        # Enforce the contract at construction time, not just by convention:
        # "missing/failed parse is unknown, never zero" means amount=None
        # must never carry a currency, and a real amount must never be 0.0
        # standing in for "no price" (SerpApi-style data might do that; this
        # dataset's `null` already means "truly unknown", so 0.0 would be a
        # real, if odd, price, not a sentinel for missing).
        if self.amount is None and self.currency is not None:
            raise ValueError("amount=None (unknown) must not carry a currency")
        if self.amount is not None and self.currency is None:
            raise ValueError("a real amount must carry a currency")


@dataclass(frozen=True)
class ParsedItemId:
    asin: str
    is_valid_format: bool


def parse_item_price(raw_price) -> ParsedPrice:
    """Typed (amount, currency) with provenance. `raw_price` is this
    dataset's own `price` field value (a float or None, straight from the
    joined metadata JSON)."""
    amount = _parse_price_value(raw_price)
    if amount is None:
        return ParsedPrice(amount=None, currency=None, provenance="missing_in_source")
    return ParsedPrice(
        amount=amount,
        currency=DATASET_CURRENCY,
        provenance="amazon_reviews_2023.meta.price",
    )


def parse_item_id(parent_asin) -> ParsedItemId:
    """Validates this dataset's own parent_asin as a real ASIN. Never falls
    back to title — titles are explicitly excluded from identity per the
    proposal ("Never merge by title alone")."""
    asin = str(parent_asin or "").strip()
    return ParsedItemId(asin=asin, is_valid_format=bool(ASIN_RE.match(asin)))


def _self_check() -> None:
    """Run the parser across the real frozen items file and cross-check its
    unknown-price count against the already-computed missingness report —
    they must agree exactly, or something is inconsistent between the two."""
    import json

    items_path = _BACKEND_DIR / "data" / "wp4" / "processed" / "items_5core.jsonl"
    if not items_path.exists():
        sys.exit(f"missing {items_path} — run wp4_freeze_dataset.py --stage join first")

    n = 0
    n_unknown_price = 0
    n_bad_asin = 0
    with open(items_path, encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            n += 1
            price = parse_item_price(obj.get("price"))
            if price.amount is None:
                n_unknown_price += 1
            item_id = parse_item_id(obj.get("parent_asin"))
            if not item_id.is_valid_format:
                n_bad_asin += 1

    print(f"parsed {n:,} items")
    print(f"unknown price: {n_unknown_price:,} ({n_unknown_price / n:.1%})")
    print(f"malformed ASIN: {n_bad_asin:,}")

    report_path = _BACKEND_DIR / "data" / "wp4" / "processed" / "missingness_report.json"
    if report_path.exists():
        expected = json.loads(report_path.read_text(encoding="utf-8"))["missing_price"]
        status = "MATCH" if expected == n_unknown_price else "MISMATCH"
        print(f"cross-check vs missingness_report.json missing_price={expected:,}: {status}")
        if status == "MISMATCH":
            sys.exit(1)


if __name__ == "__main__":
    _self_check()
