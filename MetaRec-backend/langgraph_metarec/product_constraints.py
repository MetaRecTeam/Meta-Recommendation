"""Hard-constraint model, checker, and filter for the ``product`` domain.

WP4. Three pieces:

* ``ProductConstraints`` / ``violations()`` — what a result must satisfy, and
  which constraints a given item breaks. Deterministic, dependency-free.
* ``resolve_constraints(query, preferences)`` — the constraints for a request.
  Prefers an explicit ``preferences["hard_constraints"]`` dict (populated by the
  extraction step / tests); otherwise derives a best-effort set from the loose
  preference fields and the query text.
* ``apply_hard_constraints(items, c)`` — drop violating items before ranking,
  with a relaxation ladder for when nothing survives. **Budget is never
  auto-relaxed.**
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

_PRICE_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_CURRENCIES = ("SGD", "USD", "CNY", "RMB", "EUR", "GBP", "MYR", "JPY", "AUD", "CAD", "NZD", "HKD")
_UPPER_BOUND = re.compile(r"(<=|<|under|below|no more than|至多|以内|以下|不超过|少于|低于)", re.IGNORECASE)

# Small hard-attribute vocabulary for the derive path. The extraction step is
# expected to do better; this only catches the obvious cases.
_ATTRIBUTE_TERMS = ("waterproof", "wireless", "wired", "noise cancelling", "noise canceling")


def parse_price(value: Any) -> Optional[float]:
    """Best-effort numeric price from a SerpApi/Amazon ``price`` field.

    Handles ``"$129.99"``, ``"1,299.00"``, ``"SGD 45"``, and ranges like
    ``"$10.99 - $20.99"`` (a range means variants; take the LOW end — a buyable
    variant under budget makes the listing satisfiable). ``None`` when no number.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    matches = _PRICE_NUM.findall(str(value))
    if not matches:
        return None
    return min(float(m.replace(",", "")) for m in matches)


@dataclass
class ProductConstraints:
    """Everything a product result must satisfy to be shown. Every field is
    inert at its default, so a request only sets what it actually stated."""

    budget_max: Optional[float] = None
    currency: Optional[str] = None
    # Category violation unless the item text matches at least one of these.
    category_terms: List[str] = field(default_factory=list)
    # Category violation if the item text matches any of these.
    category_exclude_terms: List[str] = field(default_factory=list)
    brand_in: List[str] = field(default_factory=list)
    brand_not_in: List[str] = field(default_factory=list)
    # Every term must appear in the item text (compatibility / attribute).
    require_terms: List[str] = field(default_factory=list)
    # No term may appear in the item text.
    exclude_terms: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProductConstraints":
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in (data or {}).items() if k in fields})

    def is_empty(self) -> bool:
        return (
            self.budget_max is None
            and not self.category_terms
            and not self.category_exclude_terms
            and not self.brand_in
            and not self.brand_not_in
            and not self.require_terms
            and not self.exclude_terms
        )


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------


def _item_text(item: Dict[str, Any]) -> str:
    parts = [
        item.get("title"),
        item.get("subtitle"),
        item.get("description"),
        " ".join(str(t) for t in (item.get("tags") or [])),
    ]
    raw = item.get("raw")
    if isinstance(raw, dict):
        parts.extend(str(raw.get(k) or "") for k in ("title", "brand", "category", "type"))
    return " ".join(p for p in parts if p).casefold()


def _brand(item: Dict[str, Any]) -> str:
    """Known brand string, casefolded, or "" when the source didn't give one.

    Deliberately does NOT fall back to a price-like ``subtitle`` — the amazon
    adapter fills subtitle with the price when brand is missing.
    """
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    brand = raw.get("brand")
    if not brand and isinstance(item.get("subtitle"), str) and not _PRICE_NUM.search(item["subtitle"]):
        brand = item["subtitle"]
    return str(brand or "").strip().casefold()


def _price_of(item: Dict[str, Any]) -> Optional[float]:
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    return parse_price(raw.get("price")) or parse_price(item.get("subtitle"))


def violations(item: Dict[str, Any], c: ProductConstraints) -> List[str]:
    """Constraint keys ``item`` violates (empty == compliant).

    Keys: ``budget``, ``category``, ``brand``, ``compat``, ``exclude``.
    """
    out: List[str] = []
    text = _item_text(item)

    if c.budget_max is not None:
        price = _price_of(item)
        if price is not None and price > c.budget_max:
            out.append("budget")

    if c.category_terms and not any(t.casefold() in text for t in c.category_terms):
        out.append("category")
    if any(t.casefold() in text for t in c.category_exclude_terms):
        out.append("category")

    if c.brand_in:
        brand = _brand(item)
        # Violation only when a *different* brand is positively readable.
        if brand and not any(b.casefold() in brand or b.casefold() in text for b in c.brand_in):
            out.append("brand")
    if c.brand_not_in:
        brand = _brand(item)
        if any(b.casefold() in brand or b.casefold() in text for b in c.brand_not_in):
            out.append("brand")

    if any(t.casefold() not in text for t in c.require_terms):
        out.append("compat")
    if any(t.casefold() in text for t in c.exclude_terms):
        out.append("exclude")

    return list(dict.fromkeys(out))


# ---------------------------------------------------------------------------
# Derivation from loose preferences + query
# ---------------------------------------------------------------------------


# Prefixed symbols first: "S$120" is Singapore dollars, not the US dollars a bare "$" means here.
_SYMBOL_CURRENCIES = (
    (re.compile(r"US\$"), "USD"),
    (re.compile(r"(?<![A-Za-z])S\$"), "SGD"),
    (re.compile(r"AU\$|(?<![A-Za-z])A\$"), "AUD"),
    (re.compile(r"CA\$|(?<![A-Za-z])C\$"), "CAD"),
    (re.compile(r"NZ\$"), "NZD"),
    (re.compile(r"HK\$"), "HKD"),
    (re.compile("€"), "EUR"),
    (re.compile("£"), "GBP"),
    (re.compile(r"\$"), "USD"),
)


def currency_from_symbol(text: Any) -> Optional[str]:
    """Currency a price or budget string names by its symbol, or None. A bare ``$`` is read as
    US dollars (the Amazon provider returns USD); a string with no symbol is left unknown rather
    than guessed."""
    value = str(text or "")
    for pattern, code in _SYMBOL_CURRENCIES:
        if pattern.search(value):
            return code
    return None


def _budget_from_text(text: str) -> Tuple[Optional[float], Optional[str]]:
    if not text or not _UPPER_BOUND.search(text):
        return None, None
    nums = _PRICE_NUM.findall(text)
    if not nums:
        return None, None
    amount = float(nums[-1].replace(",", ""))
    currency = next((cur for cur in _CURRENCIES if cur.lower() in text.lower()), None)
    if currency is None:
        currency = currency_from_symbol(text)
    return amount, currency


def _head_noun(phrase: str) -> str:
    words = re.findall(r"[a-z0-9]+", str(phrase or "").lower())
    return words[-1] if words and len(words[-1]) > 2 else ""


def derive_constraints(query: str, preferences: Dict[str, Any]) -> ProductConstraints:
    """Best-effort constraints from the normalized product preferences + query.

    Conservative on purpose — it under-constrains rather than risk dropping a
    good result. The extraction step (populating ``hard_constraints``) is where
    precision comes from.
    """
    prefs = preferences or {}
    text = " ".join(str(prefs.get(k) or "") for k in ("query", "product", "category", "use_case"))
    text = f"{query or ''} {text}".strip()

    budget_max, currency = _budget_from_text(str(prefs.get("budget") or ""))
    if budget_max is None:
        rng = prefs.get("budget_range")
        if isinstance(rng, dict) and rng.get("max") not in (None, ""):
            budget_max = parse_price(rng.get("max"))
            currency = str(rng.get("currency") or "").upper() or None
    if budget_max is None:
        budget_max, currency = _budget_from_text(query or "")

    category_terms: List[str] = []
    head = _head_noun(prefs.get("category") or prefs.get("product") or "")
    if head:
        category_terms.append(head)

    brand_in: List[str] = []
    brand = str(prefs.get("brand") or "").strip()
    if brand and brand.lower() not in ("any", "no preference"):
        brand_in.append(brand)

    lowered = text.lower()
    require_terms = [t for t in _ATTRIBUTE_TERMS if t in lowered]
    if "wireless" in require_terms and "wired" in require_terms:
        require_terms = [t for t in require_terms if t != "wired"]  # "wireless" wins

    return ProductConstraints(
        budget_max=budget_max,
        currency=currency,
        category_terms=category_terms,
        brand_in=brand_in,
        require_terms=require_terms,
    )


def resolve_constraints(query: str, preferences: Dict[str, Any]) -> ProductConstraints:
    """Constraints for a request: an explicit ``preferences['hard_constraints']``
    dict wins; otherwise derive from the loose fields."""
    explicit = (preferences or {}).get("hard_constraints")
    if isinstance(explicit, dict):
        return ProductConstraints.from_dict(explicit)
    return derive_constraints(query, preferences or {})


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------

# Relaxation order: least→most important. Budget is absent on purpose.
_RELAX_LADDER: Sequence[Tuple[str, str]] = (
    ("require_terms", "attributes"),
    ("exclude_terms", "exclusions"),
    ("category_exclude_terms", "category"),
    ("category_terms", "category"),
    ("brand_in", "brand"),
    ("brand_not_in", "brand"),
)


@dataclass
class FilterOutcome:
    kept: List[Dict[str, Any]]
    dropped: List[Tuple[Dict[str, Any], List[str]]]
    relaxed: List[str]      # constraint labels relaxed to avoid an empty result
    exhausted: bool         # True: even after relaxing all but budget, nothing fits

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "dropped": len(self.dropped),
            "dropped_reasons": sorted({k for _, ks in self.dropped for k in ks}),
            "relaxed": self.relaxed,
            "exhausted": self.exhausted,
        }


def _partition(
    items: Sequence[Dict[str, Any]], c: ProductConstraints
) -> Tuple[List[Dict[str, Any]], List[Tuple[Dict[str, Any], List[str]]]]:
    kept: List[Dict[str, Any]] = []
    dropped: List[Tuple[Dict[str, Any], List[str]]] = []
    for item in items:
        v = violations(item, c)
        if v:
            dropped.append((item, v))
        else:
            kept.append(item)
    return kept, dropped


def apply_hard_constraints(
    items: Sequence[Dict[str, Any]], c: ProductConstraints
) -> FilterOutcome:
    """Remove items that violate ``c``. If that leaves nothing, relax the
    non-budget constraints one rung at a time until something survives; if only
    budget-violating items remain, return empty (``exhausted``)."""
    if c.is_empty():
        return FilterOutcome(list(items), [], [], False)

    kept, dropped = _partition(items, c)
    if kept or not items:
        return FilterOutcome(kept, dropped, [], False)

    trial = replace(c)
    relaxed: List[str] = []
    for attr, label in _RELAX_LADDER:
        if getattr(trial, attr):
            trial = replace(trial, **{attr: []})
            relaxed.append(label)
            kept, dropped = _partition(items, trial)
            if kept:
                seen: Dict[str, None] = {}
                for r in relaxed:
                    seen.setdefault(r, None)
                return FilterOutcome(kept, dropped, list(seen), False)

    # Relaxing every non-budget constraint still left nothing: the budget alone
    # eliminates the whole pool. Report it as exhausted; the attempted
    # relaxations are moot since they changed nothing.
    _, dropped = _partition(items, replace(c))
    return FilterOutcome([], dropped, [], True)
