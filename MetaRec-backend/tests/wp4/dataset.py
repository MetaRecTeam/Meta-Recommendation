"""Load the WP4 labeled constraint dataset from ``tests/wp4/cases/*.json``.

Each case is a real or realistic product query whose candidate pool has been
hand-labeled: every candidate carries ``expect_violations`` (the constraint keys
a human judged it to break, ``[]`` for a compliant item). The pool shape matches
the ``amazon.product.search`` adapter output so it can be fed straight through
the graph.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from langgraph_metarec.product_constraints import ProductConstraints

CASES_DIR = Path(__file__).parent / "cases"

# Keys stripped from a candidate before it is handed to the (fake) search
# adapter — everything else is passed through as-is.
_LABEL_KEYS = {"expect_violations"}


@dataclass
class Case:
    id: str
    query: str
    preferences: Dict[str, Any]
    hard_constraints: ProductConstraints
    candidates: List[Dict[str, Any]]
    soft_constraints: List[str] = field(default_factory=list)
    notes: str = ""

    @property
    def preferences_with_constraints(self) -> Dict[str, Any]:
        """Preferences as they reach the graph, plus the ground-truth structured
        constraints under ``hard_constraints`` — i.e. what a perfect extraction
        step would hand the filter. Phase 1 tests the filter, not extraction."""
        return {**self.preferences, "hard_constraints": asdict(self.hard_constraints)}

    @property
    def pool(self) -> List[Dict[str, Any]]:
        """Candidate dicts as the search adapter would return them (no labels)."""
        return [{k: v for k, v in c.items() if k not in _LABEL_KEYS} for c in self.candidates]

    def labels_by_title(self) -> Dict[str, List[str]]:
        return {c["title"]: list(c.get("expect_violations") or []) for c in self.candidates}

    @property
    def pool_violator_titles(self) -> List[str]:
        return [t for t, v in self.labels_by_title().items() if v]


def load_case(path: Path) -> Case:
    data = json.loads(path.read_text(encoding="utf-8"))
    return Case(
        id=data.get("id", path.stem),
        query=data["query"],
        preferences=data.get("preferences", {}),
        hard_constraints=ProductConstraints.from_dict(data.get("hard_constraints", {})),
        candidates=data["candidates"],
        soft_constraints=data.get("soft_constraints", []),
        notes=data.get("notes", ""),
    )


def load_cases() -> List[Case]:
    cases = [load_case(p) for p in sorted(CASES_DIR.glob("*.json"))]
    if not cases:
        raise RuntimeError(f"no WP4 cases found in {CASES_DIR}")
    return cases
