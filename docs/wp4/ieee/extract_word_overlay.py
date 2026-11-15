"""Capture hand formatting from the edited Word report so md_to_ieee.py can reapply it on every rebuild.

The markdown holds the text. A few things are only formatting done by hand in Word (today: which table cells were
centred vertically, which rows may split across pages). This script reads the edited .docx and writes word-format-overlay.json next to it, keyed by the
table key used in the markdown (%table <key> | ...).

    python extract_word_overlay.py <edited.docx> <final-report-ieee.md> [word-format-overlay.json]
"""
import json
import re
import sys
import zipfile
from pathlib import Path

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
ns = {"w": W}

docx, md = Path(sys.argv[1]), Path(sys.argv[2])
out = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(__file__).with_name("word-format-overlay.json")

keys = re.findall(r"^%table\s+([^\s|]+)\s*\|", md.read_text(encoding="utf8"), flags=re.M)
root = etree.fromstring(zipfile.ZipFile(docx).read("word/document.xml"))
tables = list(root.iter("{%s}tbl" % W))
if len(keys) != len(tables):
    sys.exit(f"{len(keys)} %table blocks in the markdown but {len(tables)} tables in the docx; they must match")

overlay = {}
for key, tbl in zip(keys, tables):
    cells = {}
    for ri, tr in enumerate(tbl.findall("w:tr", ns)):
        cols = [ci for ci, tc in enumerate(tr.findall("w:tc", ns))
                if tc.find("w:tcPr/w:vAlign", ns) is not None and tc.find("w:tcPr/w:vAlign", ns).get("{%s}val" % W) == "center"]
        if cols:
            cells[str(ri)] = cols
    # rows that Word lets break across pages (the converter sets cantSplit on every body row; row 0 is the header)
    split_ok = [ri for ri, tr in enumerate(tbl.findall("w:tr", ns))
                if ri > 0 and tr.find("w:trPr/w:cantSplit", ns) is None]
    entry = {}
    if cells:
        entry["valign_center"] = cells
    if split_ok:
        entry["allow_split"] = split_ok
    if entry:
        overlay[key] = entry
out.write_text(json.dumps(overlay, indent=1), encoding="utf8")
print(f"wrote {out} | tables with hand formatting: {len(overlay)} | centred cells: "
      f"{sum(len(c) for v in overlay.values() for c in v.get('valign_center', {}).values())} | rows allowed to split: "
      f"{sum(len(v.get('allow_split', [])) for v in overlay.values())}")
