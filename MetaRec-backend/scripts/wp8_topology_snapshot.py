"""Write the frozen WP8 topology snapshots that tests/test_wp8_topology.py checks.

Run only after deliberately bumping a graph's @vN in
langgraph_metarec/wp8_trace/topology.py because its nodes or edges changed.

Usage (from MetaRec-backend/):
    ../.venv/bin/python scripts/wp8_topology_snapshot.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from langgraph_metarec.wp8_trace.topology import topology_definitions  # noqa: E402

SNAPSHOT_DIR = BACKEND_DIR / "tests" / "fixtures" / "wp8" / "topology"


def main() -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    for stale in SNAPSHOT_DIR.glob("*.json"):
        stale.unlink()
    for definition in topology_definitions():
        path = SNAPSHOT_DIR / f"{definition.graph_definition_id}.json"
        path.write_text(json.dumps(definition.model_dump(mode="json"), indent=2) + "\n")
        print("wrote", path.relative_to(BACKEND_DIR))


if __name__ == "__main__":
    main()
