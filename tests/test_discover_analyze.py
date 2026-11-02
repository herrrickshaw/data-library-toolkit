"""Characterisation of discover.scan/insert_new and analyze.generate using a
fake ``rclone`` shell script (no network). Goldens captured from the original code."""
from __future__ import annotations

import json
import stat
from pathlib import Path

from dlt import analyze, config, convert, discover
from dlt.catalog import connect

GOLDEN = Path(__file__).parent / "golden" / "discover_analyze.json"

FAKE_RCLONE = r"""#!/bin/sh
# usage: rclone lsf <remote> [--dirs-only|--files-only] [-R] ...
remote="$2"
case "$*" in
  *--dirs-only*)
    case "$remote" in
      fake:) printf 'docs/\nskipme/\nslow/\n';;
      fake:slow) printf 'a\nb\n';;
      *) ;;
    esac;;
  *-R*)
    case "$remote" in
      fake:docs) printf 'x.pdf\t10\t2024-01-02 03:04:05\nsub/y.DOCX\t20\t2024-02-03 04:05:06\nz.txt\t1\t2024-01-01 00:00:00\nbak.zip\t5\t2024-01-01 00:00:00\n';;
      fake:slow) exit 1;;
      fake:slow/a) printf 'p.pptx\t1\tbad-date\n';;
      fake:slow/b) printf 'q.xls\t2\t2023-05-06 07:08:09\n';;
      *) exit 1;;
    esac;;
  *) printf 'top.epub\t3\t2022-01-01 00:00:00\n';;
esac
"""


def test_discover_and_analyze_match_golden(isolated_config, tmp_path, monkeypatch):
    fake = tmp_path / "rclone"
    fake.write_text(FAKE_RCLONE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(config, "RCLONE_BIN", str(fake))
    monkeypatch.setattr(config, "SKIP_FOLDERS", {"skipme"})
    monkeypatch.setattr(discover, "CHECKPOINT_FILE", config.DATA_DIR / "cp.jsonl")

    logs: list = []
    found, archives, skipped = discover.scan(remotes=["fake:"], log=logs.append)
    con = connect()
    n = discover.insert_new(con, found, log=logs.append)
    n_again = discover.insert_new(con, found, log=logs.append)
    docs = con.execute(
        "select item_key,title,path,source,category,topic,filetype,mtime from documents order by path"
    ).fetchall()
    out = analyze.generate(con, log=logs.append)
    con.close()
    # GROUP BY tie order is not deterministic in DuckDB, so compare sorted lines.
    report = sorted(l for l in Path(out).read_text().splitlines() if not l.startswith("Generated "))
    snap = {"found": sorted(found), "archives": archives, "skipped": skipped,
            "inserted": [n, n_again], "docs": [list(d) for d in docs], "report": report,
            "logs": [str(x).replace(str(tmp_path), "<TMP>") for x in logs]}
    snap = json.loads(json.dumps(snap))
    if not GOLDEN.exists():
        GOLDEN.write_text(json.dumps(snap, indent=1, sort_keys=True))
    assert snap == json.loads(GOLDEN.read_text())


def test_pure_helpers():
    assert convert.safe_title_for("Report v1.PDF") == "Report v1"
    assert convert.safe_title_for(None) == "untitled"
    assert convert.safe_title_for("a/b:c") == "a_b_c"
    assert len(convert.safe_title_for("x" * 200)) == 80
    assert convert.ext_for("dir/file.PDF") == ".PDF"
    assert convert.ext_for("noext") == ".bin"
    assert convert._safe_dirname("dropbox:") == "dropbox_"
    assert discover.item_key_for("s:", "p") == discover.item_key_for("s:", "p")
    assert discover.item_key_for("s:", "p").startswith("AUTO_")
    assert discover._mtime_ms("2024-01-02 03:04:05") == 1704164645000
    assert discover._mtime_ms("garbage") is None
