"""B0 strict-API golden fixtures (WP4 acceptance criterion 1).

With METAREC_PRODUCT_RANKER unset or `legacy`, the product output must equal the
frozen B0 payload exactly, after JSON round-tripping. No network.
"""

import json

import pytest

from tests.wp4.dataset import load_cases
from tests.wp4.freeze_b0 import b0_strict_api, golden_path

CASES = load_cases()


def _round_trip(items):
    return json.loads(json.dumps(items, ensure_ascii=False))


@pytest.mark.backend_unit
def test_every_case_has_a_golden_file():
    missing = [c.id for c in CASES if not golden_path(c.id).exists()]
    assert not missing, f"no B0 golden file for: {missing} (run python -m tests.wp4.freeze_b0)"


@pytest.mark.backend_unit
@pytest.mark.parametrize("flag", [None, "legacy"])
@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_feature_off_matches_b0_strict_api_golden(monkeypatch, case, flag):
    if flag is None:
        monkeypatch.delenv("METAREC_PRODUCT_RANKER", raising=False)
    else:
        monkeypatch.setenv("METAREC_PRODUCT_RANKER", flag)

    golden = json.loads(golden_path(case.id).read_text(encoding="utf-8"))
    assert _round_trip(b0_strict_api(case)) == golden["items"], case.id


@pytest.mark.backend_unit
def test_golden_files_detect_a_behaviour_change(monkeypatch):
    """Sanity check on the fixtures themselves: turning the ranker on for a case
    that has over-budget items must NOT equal the frozen B0 payload."""
    monkeypatch.setenv("METAREC_PRODUCT_RANKER", "domain_v1")
    case = next(c for c in CASES if c.id == "case_06_monitor")
    golden = json.loads(golden_path(case.id).read_text(encoding="utf-8"))
    assert _round_trip(b0_strict_api(case)) != golden["items"]
