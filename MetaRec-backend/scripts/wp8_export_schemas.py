"""Export the checked-in WP8 JSON Schemas (2020-12) from the Pydantic models.

tests/test_wp8_event_contract.py fails if a checked-in schema differs from
what the models generate, so run this after any contract model change.

Usage (from MetaRec-backend/):
    ../.venv/bin/python scripts/wp8_export_schemas.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

BACKEND_DIR = Path(__file__).resolve().parents[1]
SCHEMA_DIR = BACKEND_DIR.parent / "contracts" / "wp8"
sys.path.insert(0, str(BACKEND_DIR))

from langgraph_metarec.wp8_trace import models  # noqa: E402

# file stem -> model. Member B adds the config, page and diff models here.
SCHEMAS = {
    "graph-run-event-v1": models.GraphRunEventV1,
    "graph-definition-v1": models.GraphDefinition,
    "graph-instance-v1": models.GraphInstance,
}


def build_schema(stem: str) -> Dict[str, Any]:
    schema = models.contract_json_schema(SCHEMAS[stem])
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"metarec:wp8/{stem}", **schema}


def render(stem: str) -> str:
    return json.dumps(build_schema(stem), indent=2, sort_keys=True) + "\n"


def main() -> None:
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    for stem in SCHEMAS:
        path = SCHEMA_DIR / f"{stem}.schema.json"
        path.write_text(render(stem))
        print("wrote", path.relative_to(BACKEND_DIR.parent))


if __name__ == "__main__":
    main()
