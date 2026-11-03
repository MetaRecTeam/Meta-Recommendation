"""Keep docs/wp8/CONTRACT.md from drifting away from the models it documents.

A reference page nobody checks is worse than no page: it tells a reader a
field exists long after it was renamed. These tests parse the page and compare
it against the live models, so the page cannot go stale without the suite
going red.
"""
import inspect
import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from langgraph_metarec.wp8_trace import models as trace_models

pytestmark = pytest.mark.backend_unit

DOC = Path(__file__).resolve().parents[1] / "docs" / "wp8" / "CONTRACT.md"

# **`ShapeName`** · Member X · N fields  ... then the next ```text block.
_ENTRY = re.compile(
    r"\*\*`(?P<name>\w+)`\*\*\s*·\s*Member\s+(?P<owner>[AB])\s*·\s*(?P<count>\d+)\s+fields?\s*"
    r"\n+```text\n(?P<body>.*?)\n```",
    re.DOTALL,
)


def _documented() -> dict[str, dict]:
    text = DOC.read_text(encoding="utf-8")
    entries: dict[str, dict] = {}
    for match in _ENTRY.finditer(text):
        body = match.group("body").strip()
        assert body.startswith("{") and body.endswith("}"), (
            f"{match.group('name')}: a field list is written as {{ a; b; c }}"
        )
        fields = []
        for token in body[1:-1].split(";"):
            token = token.strip()
            if not token:
                continue
            # "events: GraphRunEventV1[]" and "correlation: TraceCorrelation?"
            # document the field's type; only the field name is compared.
            fields.append(token.split(":", 1)[0].strip().rstrip("?[]"))
        name = match.group("name")
        assert name not in entries, f"{name} is documented twice"
        entries[name] = {
            "owner": match.group("owner"),
            "count": int(match.group("count")),
            "fields": fields,
        }
    return entries


def _models() -> dict[str, type[BaseModel]]:
    return {
        name: obj
        for name, obj in vars(trace_models).items()
        if inspect.isclass(obj)
        and issubclass(obj, BaseModel)
        and obj is not BaseModel
        and not name.startswith("_")
    }


DOCUMENTED = _documented()
MODELS = _models()


def test_doc_exists_and_parses():
    assert DOCUMENTED, f"no shapes parsed out of {DOC.name}"


def test_every_model_is_documented():
    missing = sorted(set(MODELS) - set(DOCUMENTED))
    assert not missing, f"add these shapes to docs/wp8/CONTRACT.md: {missing}"


def test_no_documented_shape_has_been_deleted():
    extra = sorted(set(DOCUMENTED) - set(MODELS))
    assert not extra, f"docs/wp8/CONTRACT.md documents shapes that no longer exist: {extra}"


@pytest.mark.parametrize("name", sorted(DOCUMENTED))
def test_documented_fields_match_the_model(name):
    model = MODELS.get(name)
    if model is None:
        pytest.skip("covered by test_no_documented_shape_has_been_deleted")
    assert DOCUMENTED[name]["fields"] == list(model.model_fields), (
        f"{name}: CONTRACT.md lists the fields in a different order or spelling "
        f"than models.py"
    )


@pytest.mark.parametrize("name", sorted(DOCUMENTED))
def test_stated_field_count_is_right(name):
    assert DOCUMENTED[name]["count"] == len(DOCUMENTED[name]["fields"]), (
        f"{name}: the '· N fields' label disagrees with the list beneath it"
    )


def test_ownership_split_is_ten_and_sixteen():
    owners = [entry["owner"] for entry in DOCUMENTED.values()]
    assert owners.count("B") == 10, "Member B owns ten shapes"
    assert owners.count("A") == 16, "Member A owns sixteen shapes"


def test_shape_total_matches_the_headline():
    text = DOC.read_text(encoding="utf-8")
    assert f"**{len(MODELS)} shapes.**" in text, (
        f"CONTRACT.md's headline count is stale; there are {len(MODELS)} shapes"
    )


@pytest.mark.parametrize(
    "constant",
    [
        "MAX_EVENTS", "MAX_EVENT_BYTES", "MAX_TRACE_BYTES", "MAX_STRING_CHARS",
        "MAX_COLLECTION_ITEMS", "MAX_NESTING_DEPTH", "MAX_RETENTION_DAYS",
        "DEFAULT_RETENTION_DAYS", "MAX_PAGE_EVENTS", "DEFAULT_PAGE_EVENTS",
        "MAX_PAGE_BYTES",
    ],
)
def test_hard_cap_table_names_a_real_constant(constant):
    assert hasattr(trace_models, constant)
    assert f"`{constant}`" in DOC.read_text(encoding="utf-8"), (
        f"{constant} is missing from the hard-caps table"
    )


@pytest.mark.parametrize(
    "cap, rendered",
    [
        ("MAX_EVENTS", "5,000"),
        ("MAX_EVENT_BYTES", "16,384"),
        ("MAX_TRACE_BYTES", "2,097,152"),
        ("MAX_STRING_CHARS", "512"),
        ("MAX_COLLECTION_ITEMS", "50"),
        ("MAX_NESTING_DEPTH", "6"),
        ("MAX_PAGE_EVENTS", "500"),
        ("MAX_PAGE_BYTES", "524,288"),
    ],
)
def test_hard_cap_values_are_printed_correctly(cap, rendered):
    assert f"{getattr(trace_models, cap):,}" == rendered
    assert rendered in DOC.read_text(encoding="utf-8")
