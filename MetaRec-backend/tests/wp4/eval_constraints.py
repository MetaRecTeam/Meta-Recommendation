"""Phase 0 harness: run every WP4 case through the live graph and measure how
often hard-constraint-violating products are surfaced.

Run standalone for the report::

    python -m tests.wp4.eval_constraints

or import ``evaluate()`` from the pytest baseline test.

Method: a fake ``amazon.product.search`` adapter returns the case's hand-labeled
candidate pool, so the graph's real normalize/dedupe/rank path decides what the
user would see. We then score the surfaced items against the human labels
(``expect_violations``) and, independently, against ``constraints.violations()``
so a drift between the two shows up.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from langgraph_metarec.graphs.generic_graph import (  # noqa: E402
    GenericGraphAdapters,
    run_generic_domain_graph,
)
from langgraph_metarec.tool_registry import ToolRegistry, ToolSpec  # noqa: E402

from langgraph_metarec.product_constraints import violations  # noqa: E402

from tests.wp4.dataset import Case, load_cases  # noqa: E402

PRODUCT_TAGS = ["#thing", "#product", "#shopping"]


def _registry_returning(pool: List[Dict[str, Any]]) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="amazon.product.search",
            domain="product",
            tags={"#thing", "#product", "#shopping"},
            input_schema={"type": "object"},
            output_schema={"type": "array"},
            adapter=lambda params, _pool=pool: list(_pool),
        )
    )
    return registry


@dataclass
class CaseResult:
    id: str
    surfaced_titles: List[str]
    pool_size: int
    pool_violators: int
    compliant_available: int         # pool items with an empty human label
    surfaced_violators: int          # by human label
    checker_violators: int           # by product_constraints.violations()
    first_violator_rank: Optional[int]
    constraint_filter: Optional[Dict[str, Any]] = None
    label_checker_disagreements: List[str] = field(default_factory=list)

    @property
    def leaked(self) -> int:
        return self.surfaced_violators

    @property
    def top3_violation(self) -> bool:
        return self.first_violator_rank is not None and self.first_violator_rank <= 3

    @property
    def surfaced_compliant(self) -> int:
        return len(self.surfaced_titles) - self.surfaced_violators


async def _run_case(case: Case) -> CaseResult:
    result = await run_generic_domain_graph(
        query=case.query,
        domain="product",
        preferences=case.preferences_with_constraints,
        tool_tags=PRODUCT_TAGS,
        adapters=GenericGraphAdapters(tool_registry=_registry_returning(case.pool)),
    )
    labels = case.labels_by_title()
    surfaced_titles = [it.get("title", "") for it in result.items]

    surfaced_violators = 0
    checker_violators = 0
    first_violator_rank: Optional[int] = None
    disagreements: List[str] = []

    for rank, item in enumerate(result.items, start=1):
        title = item.get("title", "")
        human = labels.get(title, [])
        checker = violations(item, case.hard_constraints)
        if human:
            surfaced_violators += 1
            if first_violator_rank is None:
                first_violator_rank = rank
        if checker:
            checker_violators += 1
        if bool(human) != bool(checker):
            disagreements.append(f"{title!r}: label={human} checker={checker}")

    pool_violators = len(case.pool_violator_titles)
    return CaseResult(
        id=case.id,
        surfaced_titles=surfaced_titles,
        pool_size=len(case.candidates),
        pool_violators=pool_violators,
        compliant_available=len(case.candidates) - pool_violators,
        surfaced_violators=surfaced_violators,
        checker_violators=checker_violators,
        first_violator_rank=first_violator_rank,
        constraint_filter=result.metadata.get("constraint_filter"),
        label_checker_disagreements=disagreements,
    )


@dataclass
class Report:
    cases: List[CaseResult]

    @property
    def total_pool_violators(self) -> int:
        return sum(c.pool_violators for c in self.cases)

    @property
    def total_leaked(self) -> int:
        return sum(c.leaked for c in self.cases)

    @property
    def leak_rate(self) -> float:
        return self.total_leaked / self.total_pool_violators if self.total_pool_violators else 0.0

    @property
    def cases_with_top3_violation(self) -> int:
        return sum(1 for c in self.cases if c.top3_violation)

    @property
    def total_compliant_retainable(self) -> int:
        return sum(min(c.compliant_available, 10) for c in self.cases)

    @property
    def total_compliant_surfaced(self) -> int:
        return sum(c.surfaced_compliant for c in self.cases)

    @property
    def false_drops(self) -> int:
        """Compliant items we could have shown but didn't."""
        return self.total_compliant_retainable - self.total_compliant_surfaced

    def render(self) -> str:
        width = 78
        lines = [
            "WP4 hard-constraint leak report",
            "=" * width,
            f"{'case':<27}{'pool_viol':>10}{'leaked':>8}{'compliant':>12}   {'filter note'}",
            "-" * width,
        ]
        for c in self.cases:
            avail = min(c.compliant_available, 10)
            cf = c.constraint_filter or {}
            if cf.get("exhausted"):
                note = "EXHAUSTED (nothing within budget)"
            elif cf.get("relaxed"):
                note = "relaxed: " + ",".join(cf["relaxed"])
            else:
                note = "-"
            lines.append(
                f"{c.id:<27}{c.pool_violators:>10}{c.leaked:>8}"
                f"{f'{c.surfaced_compliant}/{avail}':>12}   {note}"
            )
        lines += [
            "-" * width,
            f"{'TOTAL':<27}{self.total_pool_violators:>10}{self.total_leaked:>8}"
            f"{f'{self.total_compliant_surfaced}/{self.total_compliant_retainable}':>12}",
            "",
            f"hard-constraint violators surfaced: {self.total_leaked}/{self.total_pool_violators}  "
            f"(leak rate {self.leak_rate:.0%})",
            f"compliant items dropped by mistake: {self.false_drops}",
            f"cases with a violator in the top 3: {self.cases_with_top3_violation}/{len(self.cases)}",
        ]
        drift = [d for c in self.cases for d in c.label_checker_disagreements]
        if drift:
            lines += ["", "label vs checker disagreements:"]
            lines += [f"  - {d}" for d in drift]
        return "\n".join(lines)


@contextlib.contextmanager
def _product_ranker(mode: str):
    old = os.environ.get("METAREC_PRODUCT_RANKER")
    os.environ["METAREC_PRODUCT_RANKER"] = mode
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("METAREC_PRODUCT_RANKER", None)
        else:
            os.environ["METAREC_PRODUCT_RANKER"] = old


def evaluate() -> Report:
    """Run every case through the real graph with the M2 (domain_v1) constraint filter on."""
    with _product_ranker("domain_v1"):
        cases = load_cases()
        return Report(cases=[asyncio.run(_run_case(c)) for c in cases])


def baseline_leak() -> str:
    """B0 reference: rank the raw normalized pool with no filter (what the
    system did before WP4) and count labeled violators in the top 10."""
    from langgraph_metarec.graphs.generic_graph import _rank_items, normalize_tool_items

    total_viol = total_leak = 0
    rows = []
    for case in load_cases():
        ranked = _rank_items(normalize_tool_items("amazon.product.search", case.pool, "product"))[:10]
        labels = case.labels_by_title()
        leaked = sum(1 for it in ranked if labels.get(it["title"]))
        pv = len(case.pool_violator_titles)
        total_viol += pv
        total_leak += leaked
        rows.append(f"{case.id:<27}{pv:>10}{leaked:>8}")
    head = f"{'case':<27}{'pool_viol':>10}{'leaked':>8}"
    return "\n".join(
        ["B0 BASELINE (no filter)", "=" * 45, head, "-" * 45, *rows, "-" * 45,
         f"{'TOTAL':<27}{total_viol:>10}{total_leak:>8}",
         "", f"leak rate {total_leak / total_viol:.0%}"]
    )


if __name__ == "__main__":
    import sys as _sys

    if "--baseline" in _sys.argv:
        print(baseline_leak())
    else:
        print("B1 (hard-constraint filter on)\n" + evaluate().render())
