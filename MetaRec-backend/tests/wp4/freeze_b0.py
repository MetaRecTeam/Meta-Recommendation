"""Freeze B0 at the strict-API level.

Acceptance criterion 1 of the WP4 proposal: with the feature off, the product
output must exactly match the B0 *strict API* fixtures, with no network access.

`b0_strict_api(case)` runs one frozen candidate pool through the real graph with
the ranker flag off, drops the internal `raw` payload the same way the API does
(`main._client_safe_item`), and validates every item against the public
`RecommendationItemAPI` model, which rejects unknown fields. The result is the
exact list of items a client would receive today.

Regenerate the golden files (only when B0 is meant to change, which it is not):

    python -m tests.wp4.freeze_b0
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from langgraph_metarec.graphs.generic_graph import (  # noqa: E402
    GenericGraphAdapters,
    run_generic_domain_graph,
)
from tests.wp4.dataset import Case, load_cases  # noqa: E402
from tests.wp4.eval_constraints import PRODUCT_TAGS, _registry_returning  # noqa: E402

GOLDEN_DIR = Path(__file__).parent / "golden"


def b0_strict_api(case: Case) -> List[Dict[str, Any]]:
    """The items a client receives for this case with the ranker flag off."""
    import main  # imported lazily: it builds the FastAPI app

    # Honours whatever METAREC_PRODUCT_RANKER is set to, so tests can prove the
    # feature-off spellings match B0 and that turning the ranker on changes it.
    result = asyncio.run(
        run_generic_domain_graph(
            query=case.query,
            domain="product",
            preferences=case.preferences,
            tool_tags=PRODUCT_TAGS,
            adapters=GenericGraphAdapters(tool_registry=_registry_returning(case.pool)),
        )
    )
    return [main.RecommendationItemAPI(**main._client_safe_item(item)).model_dump() for item in result.items]


def golden_path(case_id: str) -> Path:
    return GOLDEN_DIR / f"{case_id}.json"


def main() -> None:
    os.environ.pop("METAREC_PRODUCT_RANKER", None)  # freeze B0, never the new ranker
    GOLDEN_DIR.mkdir(exist_ok=True)
    for case in load_cases():
        items = b0_strict_api(case)
        golden_path(case.id).write_text(
            json.dumps({"case_id": case.id, "query": case.query, "items": items}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"froze {case.id}: {len(items)} items")


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    main()
