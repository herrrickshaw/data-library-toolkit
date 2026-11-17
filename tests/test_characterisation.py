"""End-to-end characterisation of convert.run() on local fixtures.

The golden file tests/golden/convert_local.json was captured from the
ORIGINAL (pre-refactor) code; the refactor must reproduce it exactly.
No network, no rclone, no soffice: documents use the ``local:`` source.
"""
from __future__ import annotations

import json
from pathlib import Path

import docx
import fitz
import openpyxl
import pptx

from dlt import convert
from dlt.catalog import connect

GOLDEN = Path(__file__).parent / "golden" / "convert_local.json"


def _make_fixtures(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    d = docx.Document()
    d.add_paragraph("Hello docx")
    d.add_paragraph("   ")
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text = "a"
    t.rows[0].cells[1].text = "b"
    d.save(str(root / "one.docx"))

    wb = openpyxl.Workbook()
    wb.active.title = "S1"
    wb.active.append(["x", 1])
    wb.active.append([None, None])
    wb.active.append(["y", None])
    wb.save(str(root / "two.xlsx"))

    p = pptx.Presentation()
    s = p.slides.add_slide(p.slide_layouts[5])
    s.shapes.title.text = "Slide title"
    p.save(str(root / "three.pptx"))

    pdf = fitz.open()
    pdf.new_page().insert_text((72, 72), "Hello pdf")
    pdf.save(str(root / "four.pdf"))
    pdf.close()

    (root / "bad.docx").write_text("not a real docx")
    (root / "blank.pdf").write_bytes(b"")
    return {n: str(root / n) for n in
            ["one.docx", "two.xlsx", "three.pptx", "four.pdf", "bad.docx", "blank.pdf"]}


def _snapshot(isolated_config, tmp_path) -> dict:
    fixtures = _make_fixtures(tmp_path / "files")
    con = connect()
    rows = []
    for i, (name, path) in enumerate(sorted(fixtures.items())):
        rows.append((f"K{i}", name, path, "local:", "zotero", "t",
                     name.rsplit(".", 1)[1], None, None, False, None, None))
    rows.append(("KARC", "inner.pdf", "a.zip!/inner.pdf", "local:", "x", "t", "pdf",
                 None, None, False, None, None))
    rows.append(("KGONE", "gone.pdf", str(tmp_path / "missing.pdf"), "local:", "x", "t", "pdf",
                 None, None, False, None, None))
    con.executemany(
        "insert into documents (item_key,title,path,source,category,topic,filetype,year,md5,"
        "is_duplicate,duplicate_of,mtime) values (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.close()

    logs: list = []
    result = convert.run(max_docs=50, log=logs.append)

    con = connect()
    db = con.execute(
        "select item_key,title,source,md_path is not null,status,chars,error,cloud_status "
        "from fulltext_md order by item_key").fetchall()
    con.close()
    md = {p.name: p.read_text() for p in sorted(isolated_config.MD_OUTPUT_DIR.glob("*.md"))}
    return {"result": result, "db": [list(r) for r in db], "md": md,
            "logs": [str(x).replace(str(tmp_path), "<TMP>") for x in logs]}


def test_convert_run_matches_golden(isolated_config, tmp_path):
    snap = _snapshot(isolated_config, tmp_path)
    snap = json.loads(json.dumps(snap).replace(str(tmp_path), "<TMP>"))
    if not GOLDEN.exists():  # first capture, run against the original code only
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(snap, indent=1, sort_keys=True))
    assert snap == json.loads(GOLDEN.read_text())
