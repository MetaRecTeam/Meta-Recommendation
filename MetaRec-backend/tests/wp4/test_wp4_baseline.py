"""WP4 acceptance tests — the hard-constraint filter, exercised through the
real graph over the labeled dataset.

History: B0 (no filter) surfaced 52/52 labeled violators — see the
`python -m tests.wp4.eval_constraints --baseline` run and the project log.
"""

import asyncio

import pytest

from langgraph_metarec.graphs.generic_graph import (
    GenericGraphAdapters,
    _rank_items,
    normalize_tool_items,
    run_generic_domain_graph,
)
from langgraph_metarec.product_constraints import violations
from tests.wp4.dataset import load_cases
from tests.wp4.eval_constraints import PRODUCT_TAGS, _registry_returning, evaluate


@pytest.mark.backend_unit
def test_labels_match_checker():
    """Keep `violations()` honest against human judgment: for every labeled
    candidate, the checker's verdict (violates / doesn't) must agree with the
    hand label. Runs directly on the normalized pool, no graph."""
    cases = load_cases()
    assert cases, "no cases"
    assert sum(len(c.pool_violator_titles) for c in cases) > 0, "dataset has no labeled violators"
    disagreements = []
    for case in cases:
        normalized = normalize_tool_items("amazon.product.search", case.pool, "product")
        labels = case.labels_by_title()
        for item in normalized:
            human = labels.get(item["title"], [])
            checker = violations(item, case.hard_constraints)
            if bool(human) != bool(checker):
                disagreements.append(f"{case.id} / {item['title'][:60]!r}: label={human} checker={checker}")
    assert not disagreements, "checker vs label drift:\n  " + "\n  ".join(disagreements)


@pytest.mark.backend_unit
@pytest.mark.parametrize("flag", [None, "legacy"])
def test_feature_off_matches_b0_ordering(monkeypatch, flag):
    """Acceptance criterion 1: with METAREC_PRODUCT_RANKER unset or 'legacy', the
    product output is exactly the pre-WP4 generic ranking — no filtering."""
    if flag is None:
        monkeypatch.delenv("METAREC_PRODUCT_RANKER", raising=False)
    else:
        monkeypatch.setenv("METAREC_PRODUCT_RANKER", flag)

    for case in load_cases():
        expected = [
            it["title"]
            for it in _rank_items(normalize_tool_items("amazon.product.search", case.pool, "product"))[:10]
        ]
        result = asyncio.run(
            run_generic_domain_graph(
                query=case.query,
                domain="product",
                preferences=case.preferences_with_constraints,
                tool_tags=PRODUCT_TAGS,
                adapters=GenericGraphAdapters(tool_registry=_registry_returning(case.pool)),
            )
        )
        assert [it["title"] for it in result.items] == expected, case.id
        assert result.metadata.get("constraint_filter") is None, case.id


@pytest.mark.backend_unit
def test_no_hard_constraint_violation_is_surfaced():
    """The WP4 acceptance target: with the filter on, zero labeled violators reach the user."""
    report = evaluate()
    assert report.total_leaked == 0, (
        f"{report.total_leaked}/{report.total_pool_violators} violating items still surfaced\n\n"
        + report.render()
    )


@pytest.mark.backend_unit
def test_filter_does_not_drop_compliant_items():
    """It must not over-prune: every case that has compliant candidates still
    shows them (capped at the graph's top-10)."""
    report = evaluate()
    assert report.false_drops == 0, (
        f"{report.false_drops} compliant items were dropped by the filter\n\n" + report.render()
    )


@pytest.mark.backend_unit
def test_all_violating_pool_returns_empty_not_violations():
    """Cases where no candidate satisfies the constraints (09 ssd, 13 laptop):
    the filter must return empty + an explanation, never fall back to violations."""
    report = evaluate()
    all_violating = [c for c in report.cases if c.compliant_available == 0]
    assert all_violating, "expected at least one all-violating case in the dataset"
    for case in all_violating:
        assert case.surfaced_titles == [], f"{case.id}: surfaced items despite no compliant candidate"
        assert (case.constraint_filter or {}).get("exhausted") is True, f"{case.id}: not flagged exhausted"
