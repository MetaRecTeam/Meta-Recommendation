import pytest

from langgraph_metarec.product_constraints import (
    ProductConstraints,
    apply_hard_constraints,
    derive_constraints,
    parse_price,
    resolve_constraints,
    violations,
)

pytestmark = pytest.mark.backend_unit


def _item(title, price=None, brand=None):
    return {"title": title, "subtitle": brand or price, "tags": [], "raw": {"price": price, "brand": brand}}


# --------------------------------------------------------------------------- price

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("$129.99", 129.99),
        ("1,299.00", 1299.0),
        ("SGD 45", 45.0),
        ("$10.99 - $20.99", 10.99),   # range -> low end
        ("", None),
        (None, None),
        (68, 68.0),
    ],
)
def test_parse_price(raw, expected):
    assert parse_price(raw) == expected


# ---------------------------------------------------------------------- violations

def test_budget_violation_only_when_price_known_and_over():
    c = ProductConstraints(budget_max=100)
    assert violations(_item("A", "$150"), c) == ["budget"]
    assert violations(_item("A", "$90"), c) == []
    assert violations(_item("A", None), c) == []  # unknown price -> benefit of the doubt


def test_category_include_and_exclude():
    c = ProductConstraints(category_terms=["keyboard"], category_exclude_terms=["membrane"])
    assert violations(_item("Mechanical Keyboard"), c) == []
    assert violations(_item("Wireless Mouse"), c) == ["category"]
    assert violations(_item("Membrane Keyboard"), c) == ["category"]


def test_brand_in_needs_positive_evidence_of_other_brand():
    c = ProductConstraints(brand_in=["Sony"])
    assert violations(_item("WH-1000XM5", brand="Sony"), c) == []
    assert violations(_item("QuietComfort", brand="Bose"), c) == ["brand"]
    assert violations(_item("WH-1000XM5", brand=None), c) == []          # unknown -> allowed
    assert violations(_item("Sony WH-1000XM5", brand=None), c) == []     # title carries it


def test_brand_not_in_matches_title_too():
    c = ProductConstraints(brand_not_in=["Apple"])
    assert violations(_item("Apple AirPods 4", brand=None), c) == ["brand"]
    assert violations(_item("Galaxy Buds", brand="Samsung"), c) == []


def test_require_and_exclude_terms():
    assert violations(_item("Wired Headphones"), ProductConstraints(require_terms=["wireless"])) == ["compat"]
    assert violations(_item("Wireless Headphones"), ProductConstraints(require_terms=["wireless"])) == []
    assert violations(_item("Refurbished unit"), ProductConstraints(exclude_terms=["refurbished"])) == ["exclude"]


# ---------------------------------------------------------------- apply_hard_constraints

def test_filter_drops_only_violators():
    items = [_item("Cheap Keyboard", "$40"), _item("Pricey Keyboard", "$200")]
    out = apply_hard_constraints(items, ProductConstraints(budget_max=100))
    assert [i["title"] for i in out.kept] == ["Cheap Keyboard"]
    assert out.dropped[0][1] == ["budget"]
    assert out.relaxed == [] and out.exhausted is False


def test_empty_constraints_pass_everything_through():
    items = [_item("A"), _item("B")]
    out = apply_hard_constraints(items, ProductConstraints())
    assert out.kept == items and out.exhausted is False


def test_relaxation_ladder_drops_attribute_before_giving_up():
    # Every item is over the *stated* category but within budget; relaxing the
    # category constraint rescues them.
    items = [_item("Portable Monitor", "$120"), _item("Portable Monitor 2", "$130")]
    c = ProductConstraints(budget_max=200, category_terms=["desk"])
    out = apply_hard_constraints(items, c)
    assert len(out.kept) == 2
    assert out.relaxed == ["category"] and out.exhausted is False


def test_budget_is_never_relaxed():
    items = [_item("Gaming Laptop", "$900"), _item("Gaming Laptop 2", "$1200")]
    c = ProductConstraints(budget_max=500, category_terms=["laptop"], require_terms=["rtx"])
    out = apply_hard_constraints(items, c)
    assert out.kept == []
    assert out.exhausted is True


def test_exhausted_reports_no_relaxed_since_they_did_not_help():
    items = [_item("X", "$999")]
    out = apply_hard_constraints(items, ProductConstraints(budget_max=10, category_terms=["x"]))
    assert out.exhausted is True and out.relaxed == []


# ------------------------------------------------------------------- derive/resolve

def test_derive_budget_only_with_upper_bound_semantics():
    assert derive_constraints("", {"budget": "< 120 USD"}).budget_max == 120.0
    assert derive_constraints("", {"budget": "under 60"}).budget_max == 60.0
    assert derive_constraints("", {"budget": "120 USD"}).budget_max is None  # "around 120", not a ceiling


def test_derive_category_head_noun_and_brand_and_attributes():
    c = derive_constraints("waterproof wireless speaker under 30 USD",
                           {"product": "bluetooth speaker", "category": "portable speaker", "brand": "JBL"})
    assert c.category_terms == ["speaker"]
    assert c.brand_in == ["JBL"]
    assert "waterproof" in c.require_terms and "wireless" in c.require_terms


def test_derive_wireless_beats_wired():
    c = derive_constraints("wireless (not wired) mouse", {})
    assert "wireless" in c.require_terms and "wired" not in c.require_terms


def test_resolve_prefers_explicit_hard_constraints():
    prefs = {"budget": "< 999 USD", "hard_constraints": {"budget_max": 50, "category_terms": ["mouse"]}}
    c = resolve_constraints("anything", prefs)
    assert c.budget_max == 50 and c.category_terms == ["mouse"]
