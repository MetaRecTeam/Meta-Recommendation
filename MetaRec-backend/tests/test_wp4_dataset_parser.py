"""Unit tests for the WP4 D1 ID/price parser (scripts/wp4_dataset_parser.py).

Covers the proposal's exact contract: "Missing/failed parse is unknown,
never zero" and "Prefer ASIN when available ... never merge by title alone."
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from wp4_dataset_parser import (  # noqa: E402
    DATASET_CURRENCY,
    ParsedPrice,
    parse_item_id,
    parse_item_price,
)

pytestmark = pytest.mark.backend_unit


# --------------------------------------------------------------------------- price

def test_real_price_gets_amount_and_currency():
    result = parse_item_price(24.13)
    assert result.amount == 24.13
    assert result.currency == DATASET_CURRENCY


def test_missing_price_is_unknown_not_zero():
    result = parse_item_price(None)
    assert result.amount is None
    assert result.currency is None  # never a currency attached to "unknown"


def test_zero_price_is_a_real_price_not_treated_as_missing():
    # 0.0 is a legitimate (if unusual) price value in this dataset's schema,
    # distinct from None. The parser must not silently coerce it to "unknown".
    result = parse_item_price(0.0)
    assert result.amount == 0.0
    assert result.currency == DATASET_CURRENCY


def test_provenance_differs_between_real_and_missing():
    assert parse_item_price(9.99).provenance == "amazon_reviews_2023.meta.price"
    assert parse_item_price(None).provenance == "missing_in_source"


def test_parsed_price_rejects_inconsistent_construction():
    # amount=None must never carry a currency, and vice versa — this is the
    # literal "unknown, never zero" contract enforced structurally.
    with pytest.raises(ValueError):
        ParsedPrice(amount=None, currency="USD", provenance="x")
    with pytest.raises(ValueError):
        ParsedPrice(amount=12.0, currency=None, provenance="x")


# --------------------------------------------------------------------------- identity

def test_well_formed_asin_is_valid():
    result = parse_item_id("B01MZ3SD2X")
    assert result.asin == "B01MZ3SD2X"
    assert result.is_valid_format is True


def test_malformed_asin_is_flagged_not_silently_accepted():
    result = parse_item_id("not-an-asin")
    assert result.is_valid_format is False


def test_empty_or_none_asin_is_flagged():
    assert parse_item_id(None).is_valid_format is False
    assert parse_item_id("").is_valid_format is False


def test_asin_identity_never_falls_back_to_title():
    # parse_item_id's signature only accepts the ASIN — there is no title
    # parameter at all, so merging by title is structurally impossible here,
    # not just avoided by convention.
    import inspect

    sig = inspect.signature(parse_item_id)
    assert list(sig.parameters) == ["parent_asin"]
