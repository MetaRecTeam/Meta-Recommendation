"""WP4 D4: M2 in the live request path. Every test runs without a network, a database or a key.

What these tests pin down, from the proposal's acceptance evidence:
  * the flag accepts exactly ``legacy`` or ``domain_v1`` and a bad value stops startup;
  * with the flag off the ranker, the history seam and the ASIN are never used;
  * with it on, every ranked or excluded id comes from the input, nothing violates a stated
    hard constraint, the unknown-price policy holds, and nothing new reaches a public field;
  * an error, a timeout, an empty output, or a duplicate / unknown id falls back to the
    original order (and the hard gate still applies);
  * the signed-in user's history only adds affinity; it never overrides a constraint.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from langgraph_metarec.graphs import generic_graph
from langgraph_metarec.graphs.generic_graph import (
    GenericGraphAdapters,
    _rank_items,
    normalize_tool_items,
    run_generic_domain_graph,
)
from langgraph_metarec.product_constraints import ProductConstraints, resolve_constraints
from langgraph_metarec.product_ranker_live import (
    PRODUCT_RANKER_ENV,
    RankerRequestV1,
    RankerResultV1,
    asin_from_link,
    asin_of,
    history_from_records,
    rank_live,
    validate_product_ranker_setting,
)
from langgraph_metarec.product_ranker_m2 import REASON_CODE_ALLOW_LIST, M2Score
from tests.wp4.dataset import load_cases
from tests.wp4.eval_constraints import PRODUCT_TAGS, _registry_returning

pytestmark = pytest.mark.backend_unit

CASES = load_cases()
SIX_COMPONENTS = {"eligibility", "text_relevance", "attribute_match", "collaborative", "popularity", "tail_adjustment"}


def _graph(case, adapters=None, **kwargs):
    adapters = adapters or GenericGraphAdapters(tool_registry=_registry_returning(case.pool))
    return asyncio.run(
        run_generic_domain_graph(
            query=kwargs.pop("query", case.query),
            domain="product",
            preferences=kwargs.pop("preferences", case.preferences_with_constraints),
            tool_tags=PRODUCT_TAGS,
            adapters=adapters,
            task_id=kwargs.pop("task_id", None),
        )
    )


def _provider_item(title, price, reviews, asin=None, brand=None, rating=4.4, link=None):
    asin_part = asin or "B000000000"
    return {
        "title": title,
        "brand": brand,
        "link": link or f"https://www.amazon.com/dp/{asin_part}",
        "rating": rating,
        "reviews": reviews,
        "price": price,
        "thumbnail": "https://example.test/t.jpg",
        "source": "amazon",
        **({"asin": asin} if asin else {}),
    }


def _items(pool):
    return _rank_items(normalize_tool_items("amazon.product.search", pool, "product"))


def _record(action, item_id, title, url, event_id="e", when=None):
    return SimpleNamespace(
        event_id=event_id,
        domain="product",
        item_id=item_id,
        action=action,
        result_id=None,
        occurred_at=when or datetime(2026, 10, 1, tzinfo=timezone.utc),
        payload={"item": {"title": title, "url": url}},
    )


# ---------------------------------------------------------------------------
# The flag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value, expected", [(None, "legacy"), ("", "legacy"), ("legacy", "legacy"), ("domain_v1", "domain_v1")])
def test_flag_accepts_exactly_the_two_modes(value, expected):
    env = {} if value is None else {PRODUCT_RANKER_ENV: value}
    assert validate_product_ranker_setting(env) == expected


@pytest.mark.parametrize("bad", ["domain_v2", "LEGACY", "Domain_V1", " domain_v1", "domain_v1 ", "on", "true", "0", "m2"])
def test_flag_rejects_anything_else(bad):
    with pytest.raises(ValueError):
        validate_product_ranker_setting({PRODUCT_RANKER_ENV: bad})


def test_a_bad_value_stops_startup(monkeypatch):
    import main

    monkeypatch.setenv(PRODUCT_RANKER_ENV, "domain_v2")

    async def start():
        async with main._lifespan(main.app):
            pass

    with pytest.raises(ValueError):
        asyncio.run(start())


# ---------------------------------------------------------------------------
# Flag off: nothing of D4 is touched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", [None, "legacy"])
def test_legacy_never_calls_the_ranker_or_the_history_seam(monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv(PRODUCT_RANKER_ENV, raising=False)
    else:
        monkeypatch.setenv(PRODUCT_RANKER_ENV, flag)

    async def history_must_not_be_read():
        raise AssertionError("the history seam was read with the flag off")

    def scorer_must_not_run(**_):
        raise AssertionError("the ranker ran with the flag off")

    case = CASES[0]
    adapters = GenericGraphAdapters(
        tool_registry=_registry_returning(case.pool),
        ranker_history=history_must_not_be_read,
        ranker_scorer=scorer_must_not_run,
    )
    result = _graph(case, adapters)
    assert result.metadata["product_ranker"] is None
    expected = [i["title"] for i in _items(case.pool)[:10]]
    assert [i["title"] for i in result.items] == expected


def test_legacy_keeps_the_title_hash_id_even_when_the_provider_returns_an_asin(monkeypatch):
    monkeypatch.delenv(PRODUCT_RANKER_ENV, raising=False)
    pool = [_provider_item("Quiet keyboard", "$50", 120, asin="B07P2RN1RN")]
    (item,) = normalize_tool_items("amazon.product.search", pool, "product")
    assert item["id"].startswith("product_") and "B07P2RN1RN" not in item["id"]


# ---------------------------------------------------------------------------
# Flag on: the contract, the constraints, the projection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_ranked_and_excluded_cover_the_input_exactly_once(monkeypatch, case):
    monkeypatch.setenv(PRODUCT_RANKER_ENV, "domain_v1")
    result = _graph(case)
    meta = result.metadata["product_ranker"]
    contract = RankerResultV1(**{k: meta[k] for k in RankerResultV1.model_fields})
    ranked_ids = [r.item_id for r in contract.ranked]
    excluded_ids = [e.item_id for e in contract.excluded]

    input_ids = {generic_graph.candidate_id(i) for i in _items(case.pool)}
    assert set(ranked_ids) | set(excluded_ids) == input_ids
    assert len(ranked_ids + excluded_ids) == len(set(ranked_ids + excluded_ids)), "an id appears twice"
    assert [r.rank for r in contract.ranked] == list(range(1, len(ranked_ids) + 1))
    assert not meta["fell_back"]
    for r in contract.ranked:
        assert set(r.score_components) == SIX_COMPONENTS
        assert set(r.reason_codes) <= REASON_CODE_ALLOW_LIST
    for e in contract.excluded:
        assert e.reason_code in REASON_CODE_ALLOW_LIST
    assert contract.latency_ms >= 0


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_no_hard_constraint_is_violated_in_the_output(monkeypatch, case):
    from langgraph_metarec.product_constraints import violations

    monkeypatch.setenv(PRODUCT_RANKER_ENV, "domain_v1")
    result = _graph(case)
    labels = case.labels_by_title()
    assert not [i["title"] for i in result.items if labels.get(i["title"])], "a labelled violator was shown"
    assert not [i["title"] for i in result.items if violations(i, case.hard_constraints)], "the checker found a violator"


def test_the_output_only_reorders_or_removes_and_changes_nothing_but_why(monkeypatch):
    monkeypatch.setenv(PRODUCT_RANKER_ENV, "domain_v1")
    case = next(c for c in CASES if c.id == "case_10_standing_desk")
    result = _graph(case)
    originals = {i["title"]: i for i in _items(case.pool)}
    assert result.items
    for item in result.items:
        before = originals[item["title"]]
        assert {k: v for k, v in item.items() if k != "why"} == {k: v for k, v in before.items() if k != "why"}


def test_the_public_projection_gains_no_field(monkeypatch):
    import main

    case = next(c for c in CASES if c.id == "case_02_keyboard_live")
    monkeypatch.delenv(PRODUCT_RANKER_ENV, raising=False)
    off = _graph(case)
    monkeypatch.setenv(PRODUCT_RANKER_ENV, "domain_v1")
    on = _graph(case)
    api = main.RecommendationItemAPI  # rejects unknown fields
    off_fields = {frozenset(main._client_safe_item(i)) for i in off.items}
    on_fields = {frozenset(main._client_safe_item(i)) for i in on.items}
    assert on_fields == off_fields
    for item in on.items:
        dumped = api(**main._client_safe_item(item)).model_dump()
        assert not {"score", "score_components", "reason_codes", "rank"} & set(dumped)
    assert on.metadata["product_ranker"]["ranked"], "scores must live in the server metadata"


def test_why_is_a_fixed_template_over_real_fields(monkeypatch):
    monkeypatch.setenv(PRODUCT_RANKER_ENV, "domain_v1")
    case = next(c for c in CASES if c.id == "case_12_webcam")
    first = [i["why"] for i in _graph(case).items]
    second = [i["why"] for i in _graph(case).items]
    assert first == second
    assert all(w and w != "Matched the product search query." for w in first)
    monkeypatch.delenv(PRODUCT_RANKER_ENV)
    assert {i["why"] for i in _graph(case).items} == {"Matched the product search query."}


def test_ranking_is_deterministic_and_ties_keep_the_b0_order(monkeypatch):
    monkeypatch.setenv(PRODUCT_RANKER_ENV, "domain_v1")
    pool = [_provider_item(f"Plain mouse {n}", "$10", 50, asin=f"B00000000{n}") for n in range(5)]
    items = _items(pool)
    out = [rank_live(items=items, query="mouse", preferences={}, constraints=ProductConstraints()) for _ in range(2)]
    assert [i["title"] for i in out[0].items] == [i["title"] for i in out[1].items]
    assert [i["title"] for i in out[0].items] == [i["title"] for i in items]


def test_every_component_is_finite_and_the_run_is_fast(monkeypatch):
    monkeypatch.setenv(PRODUCT_RANKER_ENV, "domain_v1")
    case = next(c for c in CASES if c.id == "case_02_keyboard_live")
    t0 = time.perf_counter()
    meta = _graph(case).metadata["product_ranker"]
    assert time.perf_counter() - t0 < 2.0
    assert meta["latency_ms"] < 250, "ranker-only latency is far above the 25 ms target"
    for r in meta["ranked"]:
        assert all(abs(v) < 1e6 for v in r["score_components"].values())


# ---------------------------------------------------------------------------
# Missing price policy, live
# ---------------------------------------------------------------------------


def test_unknown_price_is_excluded_when_a_budget_is_stated_and_kept_when_not():
    pool = [
        _provider_item("Wireless mouse A", "$20", 300, asin="B0AAAAAAA1"),
        _provider_item("Wireless mouse B", None, 900, asin="B0AAAAAAA2"),
    ]
    items = _items(pool)
    with_budget = rank_live(items=items, query="wireless mouse under 50", preferences={},
                            constraints=ProductConstraints(budget_max=50.0, currency="USD"))
    assert [i["title"] for i in with_budget.items] == ["Wireless mouse A"]
    assert [(e.item_id, e.reason_code) for e in with_budget.result.excluded] == [("B0AAAAAAA2", "UNKNOWN_PRICE_STRICT_MODE")]

    no_budget = rank_live(items=items, query="wireless mouse", preferences={}, constraints=ProductConstraints())
    assert {i["title"] for i in no_budget.items} == {"Wireless mouse A", "Wireless mouse B"}
    assert "PRICE_MISSING" in no_budget.result.warnings


# ---------------------------------------------------------------------------
# ASIN identity
# ---------------------------------------------------------------------------


def test_adapter_keeps_the_asin(monkeypatch):
    from langgraph_metarec import tool_registry

    monkeypatch.setenv("SERPAPI_KEY", "test-key")
    monkeypatch.setattr(
        tool_registry,
        "_http_get_json",
        lambda *a, **k: {"organic_results": [{"title": "T", "asin": "B07P2RN1RN", "link": "https://www.amazon.com/dp/B07P2RN1RN",
                                              "price": "$9.99", "rating": 4, "reviews": 7}]},
    )
    out = tool_registry._amazon_product_search_adapter({"query": "x"})
    assert out[0]["asin"] == "B07P2RN1RN"


def test_asin_helpers():
    assert asin_from_link("https://www.amazon.com/Some-Title/dp/B07P2RN1RN/ref=sr_1_3?x=1") == "B07P2RN1RN"
    assert asin_from_link("https://example.test/no-asin-here") is None
    assert asin_of({"asin": "b07p2rn1rn"}) == "B07P2RN1RN"
    assert asin_of({"asin": "not-an-asin", "link": "https://www.amazon.com/dp/B07P2RN1RN"}) == "B07P2RN1RN"


def test_domain_v1_ids_are_asins_and_survive_a_title_change(monkeypatch):
    monkeypatch.setenv(PRODUCT_RANKER_ENV, "domain_v1")
    first = _items([_provider_item("Logitech MX Mechanical Keyboard", "$99", 400, asin="B0BKKKKKK1")])
    second = _items([_provider_item("Logitech MX Mechanical Wireless Keyboard (Graphite)", "$99", 400, asin="B0BKKKKKK1")])
    assert first[0]["id"] == second[0]["id"] == "B0BKKKKKK1"
    monkeypatch.delenv(PRODUCT_RANKER_ENV)
    legacy_first = _items([_provider_item("Logitech MX Mechanical Keyboard", "$99", 400, asin="B0BKKKKKK1")])
    legacy_second = _items([_provider_item("Logitech MX Mechanical Wireless Keyboard (Graphite)", "$99", 400, asin="B0BKKKKKK1")])
    assert legacy_first[0]["id"] != legacy_second[0]["id"], "legacy ids are the title hash, as before"


def test_the_same_asin_twice_is_one_candidate_not_a_fallback(monkeypatch):
    monkeypatch.setenv(PRODUCT_RANKER_ENV, "domain_v1")
    pool = [
        _provider_item("Desk lamp", "$30", 200, asin="B0LLLLLLL1", link="https://www.amazon.com/dp/B0LLLLLLL1?v=1"),
        _provider_item("Desk lamp LED", "$30", 150, asin="B0LLLLLLL1", link="https://www.amazon.com/dp/B0LLLLLLL1?v=2"),
        _provider_item("Floor lamp", "$60", 80, asin="B0LLLLLLL2"),
    ]
    out = rank_live(items=_items(pool), query="lamp", preferences={}, constraints=ProductConstraints())
    assert not out.fell_back
    assert len(out.items) == 2


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------


def _fallback_case():
    return next(c for c in CASES if c.id == "case_06_monitor")


def _b0_order_after_gate(case):
    constraints = resolve_constraints(case.query, case.preferences_with_constraints)
    from langgraph_metarec.product_ranker_m2 import exclusion_reason_codes

    return [i["title"] for i in _items(case.pool) if not exclusion_reason_codes(i, constraints)][:10]


def _scorer_returning(builder):
    def scorer(*, candidates, **_):
        return builder(candidates)

    return scorer


@pytest.mark.parametrize(
    "name, scorer, warning",
    [
        ("error", lambda **_: (_ for _ in ()).throw(RuntimeError("boom")), "FALLBACK_ERROR"),
        ("empty", _scorer_returning(lambda c: []), "FALLBACK_EMPTY_OUTPUT"),
        ("duplicate", _scorer_returning(lambda c: [M2Score(c[0]["parent_asin"], 1.0, {}), M2Score(c[0]["parent_asin"], 0.5, {})]),
         "FALLBACK_DUPLICATE_ID"),
        ("unknown", _scorer_returning(lambda c: [M2Score("B0UNKNOWN0", 1.0, {})] + [M2Score(x["parent_asin"], 0.0, {}) for x in c]),
         "FALLBACK_UNKNOWN_ID"),
        ("missing", _scorer_returning(lambda c: [M2Score(x["parent_asin"], 0.0, {}) for x in c[:-1]]), "FALLBACK_INCOMPLETE_OUTPUT"),
    ],
)
def test_a_failing_ranker_falls_back_to_b0_order_behind_the_gate(monkeypatch, name, scorer, warning):
    monkeypatch.setenv(PRODUCT_RANKER_ENV, "domain_v1")
    case = _fallback_case()
    adapters = GenericGraphAdapters(tool_registry=_registry_returning(case.pool), ranker_scorer=scorer)
    result = _graph(case, adapters)
    meta = result.metadata["product_ranker"]
    assert meta["fell_back"] is True and warning in meta["warnings"], name
    assert [i["title"] for i in result.items] == _b0_order_after_gate(case)
    labels = case.labels_by_title()
    assert not [i for i in result.items if labels.get(i["title"])], "a failure must not put a violator back"


def test_a_slow_ranker_times_out_without_waiting(monkeypatch):
    monkeypatch.setenv(PRODUCT_RANKER_ENV, "domain_v1")
    case = _fallback_case()

    def slow(**_):
        time.sleep(0.6)
        return []

    adapters = GenericGraphAdapters(tool_registry=_registry_returning(case.pool), ranker_scorer=slow, ranker_timeout_s=0.05)
    t0 = time.perf_counter()
    result = _graph(case, adapters)
    assert time.perf_counter() - t0 < 0.5, "the request waited for the slow ranker"
    assert "FALLBACK_TIMEOUT" in result.metadata["product_ranker"]["warnings"]
    assert [i["title"] for i in result.items] == _b0_order_after_gate(case)


def test_a_gate_that_removes_everything_is_not_a_fallback(monkeypatch):
    monkeypatch.setenv(PRODUCT_RANKER_ENV, "domain_v1")
    case = next(c for c in CASES if c.id == "case_09_ssd")
    result = _graph(case)
    meta = result.metadata["product_ranker"]
    assert result.items == [] and meta["fell_back"] is False and meta["ranked"] == []
    assert result.metadata["constraint_filter"]["exhausted"] is True
    assert any("hard constraints" in e for e in result.errors)


# ---------------------------------------------------------------------------
# The history seam
# ---------------------------------------------------------------------------


def test_history_from_records_gives_the_wire_shape_and_keeps_repeats():
    url = "https://www.amazon.com/dp/B0HHHHHHH1"
    records = [
        _record("consumed", "product_a", "HP printer paper 500 sheets", url, event_id="e1"),
        _record("consumed", "product_a", "HP printer paper 500 sheets", url, event_id="e2"),
        _record("hide", "product_b", "Cheap ink", "https://www.amazon.com/dp/B0HHHHHHH2", event_id="e3"),
    ]
    wire, events = history_from_records(records)
    assert [w["schema_version"] for w in wire] == ["item-interaction.v1"] * 3
    assert [w["action"] for w in wire] == ["consumed", "consumed", "hide"]
    assert [e.asin for e in events] == ["B0HHHHHHH1", "B0HHHHHHH1", "B0HHHHHHH2"]
    RankerRequestV1(request_id="r", query="q", candidates=[], interactions=wire)  # accepted by the contract


def test_history_adds_affinity_but_never_overrides_a_constraint():
    pool = [
        _provider_item("Canon ink cartridge black", "$25", 100, asin="B0IIIIIII1"),
        _provider_item("HP printer paper 500 sheets", "$12", 100, asin="B0IIIIIII2"),
        _provider_item("Generic stapler", "$8", 100, asin="B0IIIIIII3"),
        _provider_item("HP printer paper premium ream", "$90", 100, asin="B0IIIIIII4"),
    ]
    items = _items(pool)
    _, events = history_from_records([_record("consumed", "x", "HP printer paper 500 sheets", "https://www.amazon.com/dp/B0IIIIIII2")])
    constraints = ProductConstraints(budget_max=50.0, currency="USD")
    cold = rank_live(items=items, query="office supplies", preferences={}, constraints=constraints)
    warm = rank_live(items=items, query="office supplies", preferences={}, constraints=constraints, events=events)

    assert "COLD_START" in cold.result.warnings and "COLD_START" not in warm.result.warnings
    warm_ids = [r.item_id for r in warm.result.ranked]
    assert warm_ids[0] == "B0IIIIIII2", "the item the user already bought comes first"
    assert "B0IIIIIII4" not in warm_ids, "history must not bring back a candidate over budget"
    first = warm.result.ranked[0]
    assert "HISTORY_AFFINITY" in first.reason_codes
    assert all("POPULAR_FALLBACK" in r.reason_codes for r in cold.result.ranked)


def test_a_hidden_item_sinks_to_the_bottom_of_the_eligible_ones():
    pool = [_provider_item(f"Notebook {n}", "$5", 100, asin=f"B0JJJJJJJ{n}") for n in range(1, 4)]
    items = _items(pool)
    # B0 puts "Notebook 3" first on a tie (title descending), so only the hide can move it to the end
    assert items[0]["title"] == "Notebook 3"
    _, events = history_from_records([_record("hide", "x", "Notebook 3", "https://www.amazon.com/dp/B0JJJJJJJ3")])
    out = rank_live(items=items, query="notebook", preferences={}, constraints=ProductConstraints(), events=events)
    assert [r.item_id for r in out.result.ranked][-1] == "B0JJJJJJJ3"


def test_an_unreadable_history_is_a_cold_start_not_an_error(monkeypatch):
    monkeypatch.setenv(PRODUCT_RANKER_ENV, "domain_v1")
    case = _fallback_case()

    async def broken():
        raise RuntimeError("database down")

    adapters = GenericGraphAdapters(tool_registry=_registry_returning(case.pool), ranker_history=broken)
    result = _graph(case, adapters)
    meta = result.metadata["product_ranker"]
    assert result.items and meta["fell_back"] is False
    assert "HISTORY_UNAVAILABLE" in meta["warnings"] and "COLD_START" in meta["warnings"]


def test_the_history_loader_feeds_the_graph(monkeypatch):
    monkeypatch.setenv(PRODUCT_RANKER_ENV, "domain_v1")
    case = next(c for c in CASES if c.id == "case_12_webcam")
    first_title = _items(case.pool)[-1]["title"]
    asin = asin_from_link(next(i for i in case.pool if i["title"] == first_title).get("link")) or "B0KKKKKKK1"

    async def loader():
        return history_from_records([_record("consumed", "x", first_title, f"https://www.amazon.com/dp/{asin}")])

    adapters = GenericGraphAdapters(tool_registry=_registry_returning(case.pool), ranker_history=loader)
    meta = _graph(case, adapters).metadata["product_ranker"]
    assert "COLD_START" not in meta["warnings"]


# ---------------------------------------------------------------------------
# The contract models
# ---------------------------------------------------------------------------


def test_the_request_contract_rejects_unknown_fields_and_bad_constants():
    ok = dict(request_id="r1", query="mouse", candidates=[])
    RankerRequestV1(**ok)
    with pytest.raises(Exception):
        RankerRequestV1(**ok, user_id="never accepted from the request")
    with pytest.raises(Exception):
        RankerRequestV1(**ok, schema_version="ranker-request.v2")
    with pytest.raises(Exception):
        RankerRequestV1(**ok, domain="restaurant")
    with pytest.raises(Exception):
        RankerRequestV1(**ok, top_k=0)


def test_the_result_contract_is_exactly_the_proposals_fields():
    assert set(RankerResultV1.model_fields) == {
        "schema_version", "request_id", "ranked", "excluded", "model_name", "model_version",
        "feature_version", "warnings", "latency_ms",
    }


# ---------------------------------------------------------------------------
# Through the service: who the history is read for
# ---------------------------------------------------------------------------


def _service_run(monkeypatch, case, *, user_id, flag, records=None, task_id=None):
    import business_repositories
    from tests.conftest import make_service

    if flag is None:
        monkeypatch.delenv(PRODUCT_RANKER_ENV, raising=False)
    else:
        monkeypatch.setenv(PRODUCT_RANKER_ENV, flag)
    monkeypatch.setattr(generic_graph, "DEFAULT_TOOL_REGISTRY", _registry_returning(case.pool))
    calls: List[Any] = []

    async def fake_list_for_user(uid, **kwargs):
        calls.append((uid, kwargs))
        return records or []

    monkeypatch.setattr(business_repositories.item_interaction_repository, "list_for_user", fake_list_for_user)
    service, _ = make_service([])
    result = asyncio.run(
        service._execute_generic_domain_task(
            query=case.query,
            preferences=case.preferences_with_constraints,
            user_id=user_id,
            domain="product",
            use_online_agent=False,
            tool_tags=PRODUCT_TAGS,
            progress_callback=None,
            task_id=task_id,
        )
    )
    return result, calls


REGISTERED = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"


def test_service_reads_the_history_for_a_registered_user_only_when_the_flag_is_on(monkeypatch):
    case = next(c for c in CASES if c.id == "case_12_webcam")
    _, calls = _service_run(monkeypatch, case, user_id=REGISTERED, flag="domain_v1")
    assert [(uid, kw.get("domain")) for uid, kw in calls] == [(REGISTERED, "product")]

    _, calls = _service_run(monkeypatch, case, user_id=REGISTERED, flag=None)
    assert calls == []
    _, calls = _service_run(monkeypatch, case, user_id=REGISTERED, flag="legacy")
    assert calls == []


def test_service_treats_a_guest_as_a_cold_start(monkeypatch):
    case = next(c for c in CASES if c.id == "case_12_webcam")
    result, calls = _service_run(monkeypatch, case, user_id="default", flag="domain_v1")
    assert calls == []
    assert result.items
    assert "COLD_START" in result.metadata["product_ranker"]["warnings"]


def test_service_ranks_with_the_stored_history(monkeypatch):
    case = next(c for c in CASES if c.id == "case_12_webcam")
    monkeypatch.setenv(PRODUCT_RANKER_ENV, "domain_v1")
    target = _items(case.pool)[-1]
    asin = asin_of(target["raw"])
    records = [_record("consumed", "old-id", target["title"], f"https://www.amazon.com/dp/{asin}")]
    result, calls = _service_run(monkeypatch, case, user_id=REGISTERED, flag="domain_v1", records=records)
    assert len(calls) == 1
    ranked = {r["item_id"]: r for r in result.metadata["product_ranker"]["ranked"]}
    assert "HISTORY_AFFINITY" in ranked[asin]["reason_codes"]
    assert "COLD_START" not in result.metadata["product_ranker"]["warnings"]


def test_scores_stay_server_side_and_legacy_metadata_is_unchanged(monkeypatch):
    import main

    case = next(c for c in CASES if c.id == "case_12_webcam")
    on, _ = _service_run(monkeypatch, case, user_id=REGISTERED, flag="domain_v1")
    assert "product_ranker" in on.metadata
    assert "product_ranker" not in main.client_safe_metadata(on.metadata)
    off, _ = _service_run(monkeypatch, case, user_id=REGISTERED, flag=None)
    assert "product_ranker" not in off.metadata


# ---------------------------------------------------------------------------
# The proposal's contract, point by point
# ---------------------------------------------------------------------------

from langgraph_metarec.product_ranker_live import (  # noqa: E402
    INTERNAL_ITEM_FIELDS,
    MAX_CANDIDATES,
    MAX_HISTORY_EVENTS,
    FusedPreferencesV1,
    ItemInteractionV1Model,
    price_currency,
)


def test_a_candidate_has_exactly_the_internal_item_fields():
    item = _items([_provider_item("Desk lamp", "$30", 200, asin="B0LLLLLLL1")])[0]
    assert set(item) == INTERNAL_ITEM_FIELDS
    RankerRequestV1(request_id="r", query="lamp", candidates=[item])
    with pytest.raises(Exception):
        RankerRequestV1(request_id="r", query="lamp", candidates=[{k: v for k, v in item.items() if k != "raw"}])
    with pytest.raises(Exception):
        RankerRequestV1(request_id="r", query="lamp", candidates=[{**item, "score": 1.0}])


def test_the_candidate_list_and_the_history_are_bounded():
    item = _items([_provider_item("Desk lamp", "$30", 200, asin="B0LLLLLLL1")])[0]
    RankerRequestV1(request_id="r", query="q", candidates=[item] * MAX_CANDIDATES)
    with pytest.raises(Exception):
        RankerRequestV1(request_id="r", query="q", candidates=[item] * (MAX_CANDIDATES + 1))
    event = {"event_id": "e", "item_id": "i", "action": "save"}
    RankerRequestV1(request_id="r", query="q", candidates=[], interactions=[event] * MAX_HISTORY_EVENTS)
    with pytest.raises(Exception):
        RankerRequestV1(request_id="r", query="q", candidates=[], interactions=[event] * (MAX_HISTORY_EVENTS + 1))


def test_an_interaction_must_match_item_interaction_v1():
    ok = {"event_id": "e", "item_id": "i", "action": "consumed", "occurred_at": "2026-10-01T08:00:00+00:00"}
    ItemInteractionV1Model(**ok)
    ItemInteractionV1Model(**{**ok, "occurred_at": "2026-10-01T08:00:00Z"})
    for bad in (
        {**ok, "action": "share"},
        {**ok, "occurred_at": "2026-10-01T08:00:00"},
        {**ok, "user_id": "u"},
        {**ok, "domain": "restaurant"},
        {**ok, "schema_version": "item-interaction.v2"},
    ):
        with pytest.raises(Exception):
            ItemInteractionV1Model(**bad)


def test_the_typed_preferences_carry_the_hard_constraints():
    prefs = FusedPreferencesV1(use_case="Mac", category="keyboard", hard_constraints={"budget_max": 120.0})
    assert prefs.model_dump()["hard_constraints"] == {"budget_max": 120.0}
    with pytest.raises(Exception):
        FusedPreferencesV1(budget="120 USD")  # only the typed fields are accepted


def test_the_flag_is_read_exactly_in_the_graph_too(monkeypatch):
    for value, expected in (("domain_v1", "domain_v1"), ("legacy", "legacy"), ("DOMAIN_V1", "legacy"), (" domain_v1", "legacy")):
        monkeypatch.setenv(PRODUCT_RANKER_ENV, value)
        assert generic_graph._product_ranker() == expected


# ---------------------------------------------------------------------------
# Currency: the missing-price policy for a budget in another currency
# ---------------------------------------------------------------------------

SGD_BUDGET = ProductConstraints(budget_max=120.0, currency="SGD")


def test_a_budget_in_singapore_dollars_excludes_us_dollar_prices_when_no_table_is_pinned():
    items = _items([_provider_item("Quiet keyboard", "$80", 300, asin="B0MMMMMMM1")])
    out = rank_live(items=items, query="keyboard", preferences={}, constraints=SGD_BUDGET)
    assert out.items == []
    assert [(e.item_id, e.reason_code) for e in out.result.excluded] == [("B0MMMMMMM1", "UNKNOWN_PRICE_STRICT_MODE")]


def test_a_pinned_exchange_table_makes_the_currencies_comparable_and_says_so():
    table = {"version": "fx-test-1", "rates": {"USD": {"SGD": 1.30}}}
    items = _items(
        [
            _provider_item("Quiet keyboard", "$80", 300, asin="B0MMMMMMM1"),  # S$104: within S$120
            _provider_item("Loud keyboard", "$95", 900, asin="B0MMMMMMM2"),  # S$123.50: over
        ]
    )
    out = rank_live(items=items, query="keyboard", preferences={}, constraints=SGD_BUDGET, fx_table=table)
    assert [i["title"] for i in out.items] == ["Quiet keyboard"]
    assert any(w == "FX_CONVERTED:fx-test-1" for w in out.result.warnings)
    assert [(e.item_id, e.reason_code) for e in out.result.excluded] == [("B0MMMMMMM2", "OVER_BUDGET")]
    assert out.items[0]["raw"]["price"] == "$80", "the provider's own price must not be rewritten"


def test_a_price_with_no_symbol_is_kept_and_flagged_when_the_budget_names_a_currency():
    items = _items([_provider_item("Quiet keyboard", "80", 300, asin="B0MMMMMMM1")])
    out = rank_live(items=items, query="keyboard", preferences={}, constraints=SGD_BUDGET)
    assert len(out.items) == 1 and "CURRENCY_MISSING" in out.result.warnings


def test_matching_currencies_need_no_conversion():
    items = _items([_provider_item("Quiet keyboard", "$80", 300, asin="B0MMMMMMM1")])
    constraints = ProductConstraints(budget_max=120.0, currency="USD")
    out = rank_live(items=items, query="keyboard", preferences={}, constraints=constraints)
    assert len(out.items) == 1 and not any(w.startswith(("FX_", "CURRENCY_")) for w in out.result.warnings)


def test_price_symbols_and_prefixes_are_read_correctly():
    from langgraph_metarec.product_constraints import _budget_from_text, currency_from_symbol

    assert [currency_from_symbol(x) for x in ("$5", "US$5", "S$5", "AU$5", "CA$5", "HK$5", "45")] == [
        "USD", "USD", "SGD", "AUD", "CAD", "HKD", None,
    ]
    assert _budget_from_text("below S$120")[1] == "SGD" and _budget_from_text("below US$50")[1] == "USD"
    assert price_currency({"raw": {"price": "S$45"}}) == "SGD"
    assert price_currency({"raw": {"price": "45", "currency": "eur"}}) == "EUR"


# ---------------------------------------------------------------------------
# Injection, failure paths and bounds
# ---------------------------------------------------------------------------


def test_a_ranker_can_be_injected_through_the_adapters_without_monkeypatching(monkeypatch):
    monkeypatch.setenv(PRODUCT_RANKER_ENV, "domain_v1")
    case = _fallback_case()
    seen = {}

    def fake_ranker(*, items, **kwargs):
        seen["n"] = len(items)
        return rank_live(items=list(reversed(items)), **kwargs)

    adapters = GenericGraphAdapters(tool_registry=_registry_returning(case.pool), ranker=fake_ranker)
    result = _graph(case, adapters)
    assert seen["n"] == len(_items(case.pool))
    assert result.metadata["product_ranker"]["ranked"]


def test_a_ranker_that_raises_still_leaves_the_hard_gate_in_place(monkeypatch):
    monkeypatch.setenv(PRODUCT_RANKER_ENV, "domain_v1")
    case = _fallback_case()

    def broken(**_):
        raise RuntimeError("ranker bug")

    adapters = GenericGraphAdapters(tool_registry=_registry_returning(case.pool), ranker=broken)
    result = _graph(case, adapters)
    meta = result.metadata["product_ranker"]
    assert meta["fell_back"] is True and "FALLBACK_ERROR" in meta["warnings"]
    labels = case.labels_by_title()
    assert result.items and not [i for i in result.items if labels.get(i["title"])]


def test_a_slow_history_read_is_a_cold_start_and_does_not_hold_the_request(monkeypatch):
    monkeypatch.setenv(PRODUCT_RANKER_ENV, "domain_v1")
    case = _fallback_case()

    async def slow():
        await asyncio.sleep(2.0)
        return [], []

    adapters = GenericGraphAdapters(tool_registry=_registry_returning(case.pool), ranker_history=slow, ranker_timeout_s=0.1)
    t0 = time.perf_counter()
    result = _graph(case, adapters)
    assert time.perf_counter() - t0 < 1.0
    assert "HISTORY_UNAVAILABLE" in result.metadata["product_ranker"]["warnings"]
    assert result.items


def test_only_the_most_recent_history_is_read():
    records = [
        _record("save", f"i{n}", f"Item {n}", f"https://www.amazon.com/dp/B0{n:08d}", event_id=f"e{n}")
        for n in range(MAX_HISTORY_EVENTS + 50)
    ]
    wire, events = history_from_records(records)
    assert len(wire) == len(events) == MAX_HISTORY_EVENTS
    assert events[-1].item_id == f"i{MAX_HISTORY_EVENTS + 49}" and events[0].item_id == "i50"


def test_one_hundred_candidates_rank_within_the_latency_target():
    pool = [_provider_item(f"Mechanical keyboard model {n}", f"${20 + n}", 10 + n * 7, asin=f"B0{n:08d}") for n in range(100)]
    items = _items(pool)
    assert len(items) == 100
    constraints = ProductConstraints(budget_max=100.0, currency="USD")
    times = []
    for _ in range(40):
        out = rank_live(items=items, query="quiet mechanical keyboard for mac", preferences={"use_case": "Mac"}, constraints=constraints)
        times.append(out.result.latency_ms)
    times.sort()
    p95 = times[int(0.95 * len(times)) - 1]
    assert p95 < 50, f"p95 {p95:.1f} ms for 100 candidates is far above the 25 ms target"


# ---------------------------------------------------------------------------
# The task id of the request reaches the ranker (RankerRequestV1.task_id)
# ---------------------------------------------------------------------------


def _task_id_spy(seen):
    def spy(*, items, **kwargs):
        seen.append(kwargs.get("task_id", "NOT PASSED"))
        return rank_live(items=items, **kwargs)

    return spy


def test_the_task_id_given_to_the_graph_reaches_the_ranker(monkeypatch):
    monkeypatch.setenv(PRODUCT_RANKER_ENV, "domain_v1")
    case = _fallback_case()
    seen: List[Any] = []
    adapters = GenericGraphAdapters(tool_registry=_registry_returning(case.pool), ranker=_task_id_spy(seen))
    _graph(case, adapters, task_id="task-42")
    assert seen == ["task-42"]


def test_a_graph_run_without_a_task_id_hands_the_ranker_none(monkeypatch):
    monkeypatch.setenv(PRODUCT_RANKER_ENV, "domain_v1")
    case = _fallback_case()
    seen: List[Any] = []
    _graph(case, GenericGraphAdapters(tool_registry=_registry_returning(case.pool), ranker=_task_id_spy(seen)))
    assert seen == [None]


def test_the_service_passes_the_current_task_id_down_to_the_ranker(monkeypatch):
    seen: List[Any] = []
    real = generic_graph.rank_live

    def spy(**kwargs):
        seen.append(kwargs.get("task_id", "NOT PASSED"))
        return real(**kwargs)

    monkeypatch.setattr(generic_graph, "rank_live", spy)
    case = next(c for c in CASES if c.id == "case_12_webcam")
    _service_run(monkeypatch, case, user_id=REGISTERED, flag="domain_v1", task_id="task-77")
    assert seen == ["task-77"]
    seen.clear()
    _service_run(monkeypatch, case, user_id=REGISTERED, flag="domain_v1")
    assert seen == [None]


def test_the_task_id_is_validated_as_part_of_the_request_contract():
    item = _items([_provider_item("Desk lamp", "$30", 200, asin="B0LLLLLLL1")])[0]
    req = RankerRequestV1(request_id="r", task_id="task-42", query="lamp", candidates=[item])
    assert req.task_id == "task-42" and RankerRequestV1(request_id="r", query="lamp", candidates=[item]).task_id is None


def test_the_task_runner_hands_its_own_task_id_to_the_product_task(monkeypatch):
    """run_recommendation_task_graph is what the API calls; its task id must reach _execute_generic_domain_task."""
    from service import RecommendationResult
    from tests.conftest import make_service

    service, _ = make_service([])
    seen: List[Any] = []

    async def fake_generic_task(**kwargs):
        seen.append(kwargs.get("task_id", "NOT PASSED"))
        return RecommendationResult(restaurants=[], items=[], thinking_steps=[], confidence_score=0.4, metadata={})

    service._execute_generic_domain_task = fake_generic_task
    route = {
        "domain": "product", "execution_domain": "product", "mode": "single_domain", "status": "ready",
        "tool_tags": PRODUCT_TAGS,
        "domain_tasks": [{"domain": "product", "status": "ready", "tool_tags": PRODUCT_TAGS}],
    }
    asyncio.run(
        service.run_recommendation_task_graph(
            "task-from-the-runner", "quiet keyboard under 120 USD", {}, user_id="default", session_id="s-1", route=route
        )
    )
    assert seen == ["task-from-the-runner"]
