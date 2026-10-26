"""Render probe events (from wp8_parentage_probe.py) as a span waterfall SVG.

One panel per root run: indented run tree on the left, start/end bars on the
right, a dashed marker where the root graph ends. Graph identity is inferred
from each graph's first node, because every compiled graph reports the same
callback run name ("LangGraph").

Usage (from MetaRec-backend/):
    ../.venv/bin/python scripts/wp8_trace_waterfall.py <events.json> <out.svg> [--roots 1,2] [--title "..."]
"""
from __future__ import annotations

import argparse
import json
import sys
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from wp8_probe_support import GRAPH_NAMES, build_runs, children_of, first_node, identify  # noqa: E402

GRAPH_COLORS = {
    "request_orchestrator": "#1C7293",
    "routing_graph": "#6A4C93",
    "task_graph": "#B5791E",
    "generic_graph": "#2E7D63",
    "restaurant_graph": "#8C5A2B",
    "probe_parent_graph": "#5B6B79",
}
ERROR_RED = "#B03A2E"
ROW_H = 24
LABEL_W = 600
BAR_W = 720
PAD = 24
FONT = "Helvetica, Arial, sans-serif"


def flatten(runs, root) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    def walk(run, depth, graph_label):
        is_graph = run["name"] in GRAPH_NAMES
        if is_graph:
            graph_label = identify({**run, "first_node": first_node(runs, run["run_id"])}, runs)
        rows.append({**run, "depth": depth, "is_graph": is_graph, "graph_label": graph_label})
        for child in children_of(runs, run["run_id"]):
            walk(child, depth + 1, graph_label)

    walk(root, 0, None)
    return rows


def panel(rows, y0: float, title: str) -> (List[str], float):
    out: List[str] = []
    t_min = min(r["start_ms"] for r in rows)
    t_max = max((r["end_ms"] or r["start_ms"]) for r in rows)
    span = max(t_max - t_min, 1e-6)
    x0 = PAD + LABEL_W

    def tx(t):
        return x0 + (t - t_min) / span * BAR_W

    out.append(f'<text x="{PAD}" y="{y0 + 16}" font-size="15" font-weight="700" fill="#0B1F3A">{escape(title)}</text>')
    y = y0 + 30
    # time axis
    for i in range(5):
        t = t_min + span * i / 4
        x = tx(t)
        out.append(f'<line x1="{x:.1f}" y1="{y}" x2="{x:.1f}" y2="{y + len(rows) * ROW_H + 6}" stroke="#E3E8EE" stroke-width="1"/>')
        out.append(f'<text x="{x:.1f}" y="{y - 4}" font-size="10" fill="#5B6B79" text-anchor="middle">{t - t_min:.1f} ms</text>')
    root_end = rows[0]["end_ms"]
    index = {r["run_id"]: i for i, r in enumerate(rows)}
    for i, r in enumerate(rows):
        ry = y + i * ROW_H
        color = GRAPH_COLORS.get(r["graph_label"], "#5B6B79")
        indent = PAD + 8 + r["depth"] * 18
        if i % 2 == 0:
            out.append(f'<rect x="{PAD}" y="{ry}" width="{LABEL_W + BAR_W}" height="{ROW_H}" fill="#F7F9FC"/>')
        # tree connector to parent row
        if r["parent_run_id"] in index:
            py = y + index[r["parent_run_id"]] * ROW_H + ROW_H / 2
            px = PAD + 8 + (r["depth"] - 1) * 18 + 5
            out.append(f'<path d="M{px},{py + 6} V{ry + ROW_H / 2} H{indent - 3}" fill="none" stroke="#9AA8B6" stroke-width="1"/>')
        parent_row = rows[index[r["parent_run_id"]]] if r["parent_run_id"] in index else None
        # A non-graph run whose parent is a node (not a graph) is a conditional-edge router.
        router = not r["is_graph"] and parent_row is not None and not parent_row["is_graph"]
        outlives = bool(r["is_graph"] and r["depth"] > 0 and r["end_ms"] and root_end and r["end_ms"] > root_end)
        if r["is_graph"]:
            label = f'<tspan font-weight="700" fill="{color}">graph: {escape(r["graph_label"])}</tspan>'
            sub = f'run name "{r["name"]}" · {r["asyncio_task"] or ""}'
        else:
            style = ' font-style="italic"' if router else ""
            label = f'<tspan fill="#0B1F3A"{style}>{escape(r["name"])}</tspan>'
            sub = "(edge router)" if router else ""
        badges = []
        if outlives:
            badges.append("ends after root")
        if r["status"] == "error":
            badges.append(f'error: {r["error_class"]}')
        if r["status"] == "open":
            badges.append("never closed")
        warn = "".join(f'<tspan dx="6" fill="{ERROR_RED}" font-size="10" font-weight="700">{escape(b)}</tspan>' for b in badges)
        out.append(f'<text x="{indent}" y="{ry + 16}" font-size="12">{label}'
                   f'<tspan dx="6" fill="#8A97A6" font-size="10">{escape(sub)}</tspan>{warn}</text>')
        # bar
        start, end = r["start_ms"], r["end_ms"] or t_max
        bx, bw = tx(start), max(tx(end) - tx(start), 2)
        opacity = "1" if r["is_graph"] else ("0.35" if router else "0.55")
        stroke = ""
        if r["status"] == "error":
            stroke = f' stroke="{ERROR_RED}" stroke-width="2"'
        elif r["status"] == "open":
            stroke = f' stroke="{ERROR_RED}" stroke-width="1.5" stroke-dasharray="3 2"'
        out.append(f'<rect x="{bx:.1f}" y="{ry + 5}" width="{bw:.1f}" height="{ROW_H - 10}" rx="3" '
                   f'fill="{color}" fill-opacity="{opacity}"{stroke}/>')
    # root graph_end marker
    if root_end:
        mx = tx(root_end)
        out.append(f'<line x1="{mx:.1f}" y1="{y}" x2="{mx:.1f}" y2="{y + len(rows) * ROW_H}" stroke="#B03A2E" stroke-width="1.5" stroke-dasharray="5 4"/>')
        out.append(f'<text x="{mx - 4:.1f}" y="{y + len(rows) * ROW_H + 16}" font-size="10" fill="#B03A2E" text-anchor="end">root graph_end</text>')
    return out, y + len(rows) * ROW_H + 34


def render(events, root_indices: Optional[List[int]], title: str, panel_titles: Optional[List[str]]) -> str:
    runs = build_runs(events)
    roots = sorted((r for r in runs.values() if r["parent_run_id"] is None), key=lambda r: r["start_ms"])
    chosen = [roots[i - 1] for i in root_indices] if root_indices else roots
    body: List[str] = []
    y = PAD + 26
    for n, root in enumerate(chosen):
        rows = flatten(runs, root)
        heading = panel_titles[n] if panel_titles and n < len(panel_titles) else f"Root {n + 1}"
        part, y = panel(rows, y, heading)
        body.extend(part)
    width = PAD * 2 + LABEL_W + BAR_W
    height = y + PAD
    head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'font-family="{FONT}"><rect width="100%" height="100%" fill="#FFFFFF"/>'
            f'<text x="{PAD}" y="{PAD + 6}" font-size="18" font-weight="700" fill="#0B1F3A">{escape(title)}</text>')
    return head + "".join(body) + "</svg>"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("events")
    parser.add_argument("out")
    parser.add_argument("--roots", default="", help="1-based root indices, comma separated (default: all)")
    parser.add_argument("--title", default="Observed callback trace")
    parser.add_argument("--panel-titles", default="", help="'|'-separated titles, one per chosen root")
    args = parser.parse_args()
    events = json.loads(Path(args.events).read_text())
    indices = [int(x) for x in args.roots.split(",") if x.strip()] or None
    titles = [t for t in args.panel_titles.split("|") if t] or None
    Path(args.out).write_text(render(events, indices, args.title, titles))
    print("wrote", args.out)


if __name__ == "__main__":
    main()
