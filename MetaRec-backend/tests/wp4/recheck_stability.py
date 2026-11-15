"""D0 stability check: re-run a captured query live and compare with the frozen copy.

Answers two audit questions with evidence instead of assumption:
  * are the item identifiers (link/ASIN, title) stable for the same product across runs?
  * how much do price, rating and result order move between runs?

Spends real SerpApi credits (one call per query). From MetaRec-backend/:

    python -m tests.wp4.recheck_stability case_03_mouse case_08_keyboard_tight
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from tests.wp4.capture import _capture  # noqa: E402  (also loads .env)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

CASES_DIR = Path(__file__).parent / "cases"
ASIN = re.compile(r"/dp/([A-Z0-9]{10})")


def _asin(link: str) -> str:
    m = ASIN.search(link or "")
    return m.group(1) if m else ""


def compare(case_id: str) -> None:
    frozen = json.loads((CASES_DIR / f"{case_id}.json").read_text(encoding="utf-8"))
    live = asyncio.run(_capture(frozen["query"], frozen.get("preferences", {})))
    old = {_asin(c["link"]): c for c in frozen["candidates"] if _asin(c["link"])}
    new = {_asin(c.get("link") or ""): c for c in live if _asin(c.get("link") or "")}
    shared = sorted(set(old) & set(new))

    print(f"\n{case_id}: {frozen['query']!r}")
    print(f"  frozen {len(old)} items, live {len(new)} items, shared ASINs {len(shared)}")
    if not shared:
        print("  no overlap - cannot assess stability from this query")
        return
    title_diff = [a for a in shared if old[a]["title"] != new[a]["title"]]
    link_diff = [a for a in shared if old[a]["link"] != new[a].get("link")]
    price_diff = [a for a in shared if old[a].get("price") != new[a].get("price")]
    rating_diff = [a for a in shared if old[a].get("rating") != new[a].get("rating")]
    print(f"  of the shared items: title changed {len(title_diff)}, link changed {len(link_diff)}, "
          f"price changed {len(price_diff)}, rating changed {len(rating_diff)}")
    for a in link_diff[:3]:
        print(f"    link {a}: {old[a]['link']}  ->  {new[a].get('link')}")
    for a in price_diff[:5]:
        print(f"    price {a}: {old[a].get('price')} -> {new[a].get('price')}")
    old_order = [a for a in (_asin(c["link"]) for c in frozen["candidates"]) if a in shared]
    new_order = [a for a in (_asin(c.get("link") or "") for c in live) if a in shared]
    print(f"  relative order of shared items identical: {old_order == new_order}")


if __name__ == "__main__":
    ids = sys.argv[1:]
    if not ids:
        sys.exit("give one or more case ids, e.g. case_03_mouse")
    for case in ids:
        compare(case)
