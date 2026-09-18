"""D0 field audit: measure how complete and how stable the provider candidate data is.

Run from MetaRec-backend/ with the venv active:

    python -m tests.wp4.audit_candidates

Only the live SerpApi captures are audited. case_01 is excluded because it was
transcribed by hand from a log, so its ASINs and links are not real provider output.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from langgraph_metarec.product_constraints import parse_price  # noqa: E402

CASES_DIR = Path(__file__).parent / "cases"
EXCLUDE = {"case_01_keyboard"}
FIELDS = ["title", "brand", "price", "rating", "reviews", "link", "thumbnail"]
ASIN_IN_LINK = re.compile(r"/dp/([A-Z0-9]{10})")


def _present(value: Any) -> bool:
    return value not in (None, "", [], {})


def load_live_candidates() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in sorted(CASES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        case_id = data.get("id", path.stem)
        if case_id in EXCLUDE:
            continue
        for cand in data["candidates"]:
            rows.append({**cand, "_case": case_id})
    return rows


def pct(n: int, total: int) -> str:
    return f"{n}/{total} ({n / total:.0%})" if total else "n/a"


def main() -> None:
    rows = load_live_candidates()
    total = len(rows)
    cases = sorted({r["_case"] for r in rows})
    print(f"Audited {total} live candidates across {len(cases)} captured queries\n")

    print("FIELD PRESENCE")
    for field in FIELDS:
        have = sum(1 for r in rows if _present(r.get(field)))
        print(f"  {field:<10} present in {pct(have, total)}")

    print("\nPRICE FORMAT")
    priced = [r for r in rows if _present(r.get("price"))]
    parsed = [r for r in priced if parse_price(r["price"]) is not None]
    ranged = [r for r in priced if re.search(r"\d\s*[-–]\s*\$?\d", str(r["price"]))]
    symbols = Counter(re.sub(r"[\d.,\s]", "", str(r["price"]))[:3] or "(none)" for r in priced)
    print(f"  price present:           {pct(len(priced), total)}")
    print(f"  parseable to a number:   {pct(len(parsed), len(priced))}")
    print(f"  range like '$a - $b':    {len(ranged)}")
    print(f"  currency symbols seen:   {dict(symbols)}")
    missing_by_case = Counter(r["_case"] for r in rows if not _present(r.get("price")))
    print(f"  missing price by query:  {dict(missing_by_case) or 'none'}")

    print("\nBRAND")
    branded = [r for r in rows if _present(r.get("brand"))]
    print(f"  brand field populated:   {pct(len(branded), total)}")
    by_case = defaultdict(lambda: [0, 0])
    for r in rows:
        by_case[r["_case"]][1] += 1
        by_case[r["_case"]][0] += 1 if _present(r.get("brand")) else 0
    print("  populated per query:     " + ", ".join(f"{c.split('_')[1]}:{a}/{b}" for c, (a, b) in sorted(by_case.items())))

    print("\nIDENTITY (ASIN recoverable from the link?)")
    asins = [(ASIN_IN_LINK.search(str(r.get("link") or "")), r) for r in rows]
    with_asin = [(m.group(1), r) for m, r in asins if m]
    print(f"  link contains /dp/<ASIN>: {pct(len(with_asin), total)}")
    seen: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for asin, r in with_asin:
        seen[asin].append(r)
    repeated = {a: rs for a, rs in seen.items() if len(rs) > 1}
    print(f"  distinct ASINs:           {len(seen)}")
    print(f"  ASINs seen in >1 query:   {len(repeated)}")
    drift_title = drift_link = 0
    for asin, rs in repeated.items():
        titles = {r["title"] for r in rs}
        links = {r["link"] for r in rs}
        drift_title += len(titles) > 1
        drift_link += len(links) > 1
        print(f"    {asin}: in {[r['_case'].split('_')[1] for r in rs]} | titles differ: {len(titles) > 1} | links differ: {len(links) > 1}")
    print(f"  repeated ASINs whose title differs across queries: {drift_title}")
    print(f"  repeated ASINs whose link differs across queries:  {drift_link}")
    dup_titles = Counter(r["title"] for r in rows)
    print(f"  identical title appearing more than once:          {sum(1 for c in dup_titles.values() if c > 1)}")

    print("\nCATEGORY")
    print("  No category/type field exists in any candidate (keys seen: "
          + ", ".join(sorted({k for r in rows for k in r if not k.startswith('_') and k != 'expect_violations'})) + ")")


if __name__ == "__main__":
    main()
