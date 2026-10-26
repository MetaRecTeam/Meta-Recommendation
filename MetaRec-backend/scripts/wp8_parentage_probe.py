"""WP8 D0 baseline probe: observe real LangGraph callback parentage, no code changes.

Thin CLI over tests/wp8_probe_support.py (the same experiments the
runtime_contract tests in tests/test_wp8_parentage_baseline.py assert on).
Writes <out>/<experiment>.events.json and <out>/summary.json. Network-free.

Usage (from MetaRec-backend/):
    ../.venv/bin/python scripts/wp8_parentage_probe.py --out docs/wp8/parentage-baseline/data
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR / "tests"))

from wp8_probe_support import CallCounter, EXPERIMENTS, Gate, _hotel_chain, analyse, record  # noqa: E402


async def run_all(out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {"experiments": {}}
    for name, experiment in EXPERIMENTS.items():
        events, outcome = await record(experiment)
        (out_dir / f"{name}.events.json").write_text(json.dumps(events, indent=2))
        summary["experiments"][name] = analyse(events, outcome)

    # E6a and E6b on one time axis, for the side-by-side figure.
    inherited = json.loads((out_dir / "E6a_handoff_inherited_context.events.json").read_text())
    fresh = json.loads((out_dir / "E6b_handoff_fresh_context.events.json").read_text())
    offset = max(e["t_ms"] for e in inherited) + 5
    pair = inherited + [{**e, "t_ms": round(e["t_ms"] + offset, 3)} for e in fresh]
    (out_dir / "E6_handoff_pair.events.json").write_text(json.dumps(pair, indent=2))

    off = await _hotel_chain("u-probe-off", CallCounter(), Gate())
    on = summary["experiments"]["E3_service_hotel_chain"]["outcome"]
    summary["exactly_once"] = {"tool_calls_tracing_off": off["tool_adapter_calls"],
                               "tool_calls_tracing_on": on["tool_adapter_calls"],
                               "items_equal": off["item_titles"] == on["item_titles"]}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    return summary


def print_report(summary: dict) -> None:
    for name, rep in summary["experiments"].items():
        print(f"\n== {name}  events={rep['event_count']} tool/llm={rep['tool_or_llm_events']} integrity={rep['integrity']}")
        print(f"   outcome: {rep['outcome']}")
        for g in rep["graphs"]:
            print(f"   {g['graph']:<21} status={g['status']:<9} parent={str(g['parent_name']):<22} "
                  f"ends_after_root={g['ends_after_root']!s:<5} err={g['error_class']}")
        if rep["errors"]:
            print(f"   error runs: {rep['errors']}")
        if rep["concurrency"]["task_graphs_checked"] > 1:
            print(f"   concurrency: {rep['concurrency']}")
    print(f"\n== exactly_once: {summary['exactly_once']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="output directory, e.g. docs/wp8/parentage-baseline/data")
    print_report(asyncio.run(run_all(Path(parser.parse_args().out))))


if __name__ == "__main__":
    main()
