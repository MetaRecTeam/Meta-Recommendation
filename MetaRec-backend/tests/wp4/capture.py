"""Capture a real product-search candidate pool into a WP4 case skeleton.

Hits the live ``amazon.product.search`` adapter (real SerpApi credits) with the
default tool registry, then writes a ``cases/*.json`` skeleton you finish by
hand: fill ``hard_constraints`` and set ``expect_violations`` on each candidate.

    python -m tests.wp4.capture "quiet mechanical keyboard under 120 USD" \
        --pref product="mechanical keyboard" --pref budget="< 120 USD" \
        --out tests/wp4/cases/case_02_keyboard_live.json

Leave ``--out`` off to just print the pool.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

for _stream in (sys.stdout, sys.stderr):  # product titles carry non-cp1252 chars
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_BACKEND_DIR / ".env")

from langgraph_metarec.graphs.generic_graph import run_generic_domain_graph  # noqa: E402

PRODUCT_TAGS = ["#thing", "#product", "#shopping"]
_KEEP = ("title", "brand", "price", "rating", "reviews", "link", "thumbnail", "source")


async def _capture(query: str, preferences: dict) -> list[dict]:
    result = await run_generic_domain_graph(
        query=query, domain="product", preferences=preferences, tool_tags=PRODUCT_TAGS
    )
    raw: list[dict] = []
    for execution in result.metadata.get("executions", []):
        if execution.get("tool") == "amazon.product.search" and execution.get("success"):
            raw.extend(execution.get("output") or [])
    if not raw:  # fall back to the normalized items if the raw output was compacted away
        for item in result.items:
            src = item.get("raw") if isinstance(item.get("raw"), dict) else item
            raw.append(src)
    return raw


def _skeleton(case_id: str, query: str, preferences: dict, raw: list[dict]) -> dict:
    return {
        "id": case_id,
        "query": query,
        "preferences": preferences,
        "hard_constraints": {
            "budget_max": None,
            "currency": None,
            "category_terms": [],
            "category_exclude_terms": [],
            "brand_in": [],
            "brand_not_in": [],
            "require_terms": [],
            "exclude_terms": [],
        },
        "soft_constraints": [],
        "notes": "LIVE capture — fill hard_constraints and label every expect_violations.",
        "candidates": [
            {**{k: item.get(k) for k in _KEEP if item.get(k) is not None}, "expect_violations": []}
            for item in raw
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    parser.add_argument("--pref", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--force", action="store_true", help="overwrite an existing --out file")
    args = parser.parse_args()

    if args.out and args.out.exists() and not args.force:
        parser.error(f"{args.out} exists; pass --force to overwrite (you will lose its labels)")

    preferences = dict(p.split("=", 1) for p in args.pref)
    raw = asyncio.run(_capture(args.query, preferences))
    print(f"captured {len(raw)} candidates", file=sys.stderr)
    if not raw:
        parser.error("no candidates returned (SerpApi empty/errored) - nothing written; retry")

    case_id = args.out.stem if args.out else "case_live"
    skeleton = _skeleton(case_id, args.query, preferences, raw)
    text = json.dumps(skeleton, indent=2, ensure_ascii=False)

    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
