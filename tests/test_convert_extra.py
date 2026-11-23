"""convert.py edge paths (soffice, markitdown fallback, rclone timeouts, cloud retry, run() branches),
plus cli handlers, catalog lock retry, analyze report sections and discover helpers. All offline."""
from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dlt import analyze, catalog, cli, config, convert, discover  # noqa: E402


@pytest.fixture
def cfg(isolated_config, monkeypatch):
    monkeypatch.setattr(config, "DISK_SAFETY_MARGIN_GB", 0)
    monkeypatch.setattr(config, "CLOUD_DEST_FOLDER", "out")
    return isolated_config


def add(key, path, filetype="pdf", source="dropbox:", title=None, dup=False):
    con = catalog.connect()
    con.execute("insert into documents (item_key, title, path, source, filetype, is_duplicate) "
                "values (?,?,?,?,?,?)", [key, title or Path(path).name, path, source, filetype, dup])
    con.close()


def statuses():
    con = catalog.connect()
    out = {k: (s, c) for k, s, c in con.execute("select item_key, status, cloud_status from fulltext_md").fetchall()}
    con.close()
    return out


def cp(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# =============================== extraction ===============================

def test_markitdown_fallback_paths(monkeypatch, tmp_path):
    f = tmp_path / "x.mobi"
    f.write_bytes(b"x")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: cp(0, "converted text"))
    assert convert.extract_text(f, "mobi") == ("converted text", None)
    assert convert.extract_text(f, None) == ("converted text", None)       # no filetype -> fallback
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: cp(1, "", "x" * 600))
    text, err = convert.extract_text(f, "mobi")
    assert text is None and err == "x" * 500                                # tail of stderr, capped at 500
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: cp(2, "", ""))
    assert convert.extract_text(f, "mobi") == (None, "markitdown failed")

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("markitdown", 1)
    monkeypatch.setattr(subprocess, "run", timeout)
    assert convert.extract_text(f, "mobi") == (None, f"markitdown timeout after {config.MARKITDOWN_TIMEOUT_S}s")


def test_extract_fitz_password_protected(tmp_path):
    import fitz
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "secret")
    p = tmp_path / "enc.pdf"
    doc.save(str(p), encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
    doc.close()
    assert convert.extract_text(p, "pdf") == (None, "password-protected document")


def test_extract_pptx_and_xlsx(tmp_path):
    import openpyxl
    import pptx
    p = pptx.Presentation()
    s = p.slides.add_slide(p.slide_layouts[5])
    s.shapes.title.text = "Title here"
    tbl = s.shapes.add_table(1, 2, 0, 0, 100, 100).table
    tbl.cell(0, 0).text, tbl.cell(0, 1).text = "a", "b"
    p.slides.add_slide(p.slide_layouts[6])                                  # empty slide is skipped
    p.save(str(tmp_path / "s.pptx"))
    text, err = convert.extract_text(tmp_path / "s.pptx", "pptx")
    assert err is None and text.startswith("## Slide 1") and "Title here" in text and "a | b" in text
    assert "Slide 2" not in text

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["x", 1])
    ws.append([None, None])
    ws.append(["y", None])
    wb.create_sheet("Empty")
    wb.save(str(tmp_path / "b.xlsx"))
    text, err = convert.extract_text(tmp_path / "b.xlsx", "xlsx")
    assert text == "## Sheet: Data\nx | 1\ny | " and err is None


def test_extract_error_message_is_tail_capped(tmp_path):
    (tmp_path / "bad.docx").write_bytes(b"nope")
    text, err = convert.extract_text(tmp_path / "bad.docx", "DOCX")           # filetype is case-insensitive
    assert text is None and err.startswith("docx extraction error: ")


# =============================== soffice ===============================

def test_soffice_batch_groups_by_filter_and_collects_outputs(cfg, monkeypatch, tmp_path):
    staged = {}
    for k, name in [("A", "a.doc"), ("B", "b.ppt"), ("C", "c.xls"), ("D", "d.doc")]:
        f = tmp_path / name
        f.write_text("x")
        staged[k] = f
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        outdir = Path(cmd[cmd.index("--outdir") + 1])
        for inp in [c for c in cmd if str(c).endswith((".doc", ".ppt", ".xls"))]:
            key = Path(inp).stem
            if key == "D":
                continue                                                      # soffice "produced nothing" for D
            suffix = ".csv" if inp.endswith(".xls") else ".txt"
            (outdir / f"{key}{suffix}").write_text(f"text of {key}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    items = [("A", staged["A"], ".doc", "doc"), ("B", staged["B"], ".ppt", "ppt"),
             ("C", staged["C"], ".xls", "xls"), ("D", staged["D"], ".doc", "doc")]
    res = convert.convert_batch_soffice(items)
    assert len(calls) == 2                                                    # one call per filter group
    filters = {c[c.index("--convert-to") + 1] for c in calls}
    assert filters == {"txt:Text", "csv:Text - txt - csv (StarCalc)"}
    assert res["A"] == ("text of A", None) and res["C"] == ("text of C", None)
    assert res["D"] == (None, "soffice produced no output for this file")
    assert not (config.STAGE_DIR / "soffice_in").exists()                     # scratch dirs cleaned up
    assert convert.convert_batch_soffice([]) == {}


def test_soffice_timeout_picks_up_partial_output(cfg, monkeypatch, tmp_path):
    f = tmp_path / "a.doc"
    f.write_text("x")

    def fake_run(cmd, **kw):
        outdir = Path(cmd[cmd.index("--outdir") + 1])
        (outdir / "A.txt").write_text("partial")
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert convert.convert_batch_soffice([("A", f, ".doc", "doc")])["A"] == ("partial", None)


def test_soffice_unknown_filetype_falls_back_to_text_filter(cfg, monkeypatch, tmp_path):
    f = tmp_path / "a.odt"
    f.write_text("x")
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: seen.append(cmd))
    convert.convert_batch_soffice([("A", f, ".odt", "odt")])
    assert seen[0][seen[0].index("--convert-to") + 1] == "txt:Text"


# =============================== rclone batch I/O ===============================

def test_bulk_fetch_local_source_copies_files(cfg, tmp_path):
    src = tmp_path / "lib" / "paper.pdf"
    src.parent.mkdir()
    src.write_text("pdf")
    convert.bulk_fetch({"local:": [("K1", str(src)), ("K2", str(tmp_path / "gone.pdf"))], "empty:": []})
    dest = config.FETCH_DIR / "local_" / str(src).lstrip("/")
    assert dest.read_text() == "pdf"
    assert not (config.FETCH_DIR / "empty_").exists()


def test_bulk_fetch_rclone_command_and_timeout(cfg, monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    convert.bulk_fetch({"dropbox:": [("K1", "a/b.pdf"), ("K2", "c.pdf")]})
    cmd, kw = calls[0]
    assert cmd[1:4] == ["copy", "dropbox:", str(config.FETCH_DIR / "dropbox_")]
    assert "--files-from" in cmd and "--ignore-errors" in cmd and kw["timeout"] == config.RCLONE_TIMEOUT_S
    listfile = Path(cmd[cmd.index("--files-from") + 1])
    assert listfile.read_text() == "a/b.pdf\nc.pdf"


def test_bulk_upload_branches(cfg, monkeypatch):
    up = config.UPLOAD_DIR
    (up / "dropbox_").mkdir(parents=True)
    (up / "dropbox_" / "f.md").write_text("x")
    (up / "empty_").mkdir()
    (up / "slow_").mkdir()
    (up / "slow_" / "f.md").write_text("x")
    (up / "bad_").mkdir()
    (up / "bad_" / "f.md").write_text("x")

    def fake_run(cmd, **kw):
        if "slow:" in cmd[3]:
            raise subprocess.TimeoutExpired(cmd, 1)
        return cp(1 if "bad:" in cmd[3] else 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = convert.bulk_upload({"dropbox:", "empty:", "slow:", "bad:", "missing:"})
    assert res == {"dropbox:": True, "empty:": True, "slow:": False, "bad:": False, "missing:": True}
    monkeypatch.setattr(config, "CLOUD_DEST_FOLDER", "")
    assert convert.bulk_upload({"dropbox:"}) == {"dropbox:": True}            # copy-back disabled


# =============================== retry + run() branches ===============================

def test_retry_cloud_failures_heals_and_reports(cfg, monkeypatch, tmp_path):
    md = tmp_path / "K1_t.md"
    md.write_text("hello")
    con = catalog.connect()
    ins = "insert into fulltext_md values (?,'T','dropbox:',?,?,?,?,?,now(),?,?,null,null)"
    con.execute(ins, ["K1", "p.pdf", str(md), "ok", 5, 1.0, None, "failed"])
    con.execute(ins, ["K2", "q.pdf", "/nope.md", "ok", 5, 1.0, None, None])
    con.execute(ins, ["K3", "r.pdf", None, "convert_failed", None, None, "e", None])
    monkeypatch.setattr(convert, "bulk_upload", lambda sources: {s: True for s in sources})
    logs = []
    convert.retry_cloud_failures(con, log=logs.append)
    q = "select item_key, cloud_status, cloud_md_path from fulltext_md"
    rows = {r[0]: r[1:] for r in con.execute(q).fetchall()}
    assert rows["K1"] == ("ok", "dropbox:out/K1_T.md")
    assert rows["K2"][0] is None                                              # md file missing: not healed
    assert rows["K3"][0] is None                                              # not status=ok: untouched
    assert logs == ["  retrying cloud copy-back for 2 item(s)...", "  ...1/2 healed."]
    assert not (config.UPLOAD_DIR / "dropbox_").exists()
    logs.clear()
    con.execute("update fulltext_md set cloud_status='ok'")
    convert.retry_cloud_failures(con, log=logs.append)
    assert logs == []                                                         # nothing to retry
    con.close()


def test_retry_cloud_failures_upload_failure_keeps_status(cfg, monkeypatch, tmp_path):
    md = tmp_path / "K1.md"
    md.write_text("x")
    con = catalog.connect()
    con.execute("insert into fulltext_md values (?,'T','dropbox:','p.pdf',?,'ok',5,1.0,now(),null,'failed',null,null)",
                ["K1", str(md)])
    monkeypatch.setattr(convert, "bulk_upload", lambda sources: {s: False for s in sources})
    convert.retry_cloud_failures(con, log=lambda m: None)
    assert con.execute("select cloud_status from fulltext_md").fetchone()[0] == "failed"
    con.close()


def test_run_disk_guard_stops_before_work(cfg, monkeypatch):
    add("K1", "a.pdf")
    monkeypatch.setattr(config, "DISK_SAFETY_MARGIN_GB", 1e9)
    logs = []
    assert convert.run(log=logs.append) is None
    assert logs == ["disk safety margin hit (<1000000000.0 GB free); stopping."]
    assert statuses() == {}


def test_run_boundary_of_disk_guard(cfg, monkeypatch):
    add("K1", "a.pdf")
    monkeypatch.setattr(convert.shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(free=int(2 * 1024 ** 3)))
    monkeypatch.setattr(config, "DISK_SAFETY_MARGIN_GB", 2)
    monkeypatch.setattr(convert, "bulk_fetch", lambda items: None)           # never shell out to rclone
    assert convert.run(log=lambda m: None) is not None                        # exactly at the margin runs


def test_run_classifies_too_large_empty_and_soffice(cfg, monkeypatch, tmp_path):
    import docx
    fetch = config.FETCH_DIR / "dropbox_"
    for name in ("big.pdf", "empty.docx", "legacy.doc", "bad.xls"):
        (fetch / name).parent.mkdir(parents=True, exist_ok=True)
    (fetch / "big.pdf").write_bytes(b"x" * 200_000)
    d = docx.Document()
    d.save(str(fetch / "empty.docx"))
    (fetch / "legacy.doc").write_text("legacy")
    (fetch / "bad.xls").write_text("x")
    monkeypatch.setattr(config, "MAX_SINGLE_FILE_MB", 0.1)                    # ~100 KB: only big.pdf is too large
    for k, name, ft in [("K1", "big.pdf", "pdf"), ("K2", "empty.docx", "docx"),
                        ("K3", "legacy.doc", "doc"), ("K4", "bad.xls", "xls"), ("K5", "dup.pdf", "pdf")]:
        add(k, name, ft, dup=(k == "K5"))
    monkeypatch.setattr(convert, "bulk_fetch", lambda items: None)            # files are pre-staged above
    monkeypatch.setattr(convert, "bulk_upload", lambda sources: {s: False for s in sources})

    def fake_soffice(items):
        return {"K3": ("converted legacy text", None), "K4": (None, "boom")}

    monkeypatch.setattr(convert, "convert_batch_soffice", fake_soffice)
    # run() wipes FETCH_DIR before fetching, so stage via the bulk_fetch hook instead
    stage = {}
    for name in ("big.pdf", "empty.docx", "legacy.doc", "bad.xls"):
        stage[name] = (fetch / name).read_bytes()

    def restage(items):
        for n, b in stage.items():
            (config.FETCH_DIR / "dropbox_" / n).parent.mkdir(parents=True, exist_ok=True)
            (config.FETCH_DIR / "dropbox_" / n).write_bytes(b)

    monkeypatch.setattr(convert, "bulk_fetch", restage)
    logs = []
    res = convert.run(log=logs.append)
    st = statuses()
    assert st["K1"][0] == "too_large"
    assert st["K2"][0] == "empty_extraction"
    assert st["K3"] == ("ok", "failed")                                       # upload failed; retried next run
    assert st["K4"][0] == "convert_failed"
    assert "K5" not in st                                                     # duplicates are never converted
    assert res == {"converted": 1, "remaining": 0}
    assert any("batch-converting 2 legacy" in m for m in logs)
    assert any("1 converted (0 cloud-copied)" in m and "1 too large" in m for m in logs)


def test_run_convert_failed_and_remaining_counts(cfg, monkeypatch):
    for i in range(3):
        add(f"K{i}", f"f{i}.pdf")
    monkeypatch.setattr(convert, "bulk_fetch", lambda items: None)
    res = convert.run(max_docs=2, log=lambda m: None)
    assert res == {"converted": 0, "remaining": 1}                            # 2 fetch_failed, 1 not yet attempted
    assert {v[0] for v in statuses().values()} == {"fetch_failed"}


# =============================== catalog / analyze / cli ===============================

def test_connect_retries_on_writer_lock_then_gives_up(cfg, monkeypatch):
    calls = {"n": 0}
    real = duckdb.connect

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise duckdb.IOException("locked")
        return real(*a, **k)

    sleeps = []
    monkeypatch.setattr(catalog.duckdb, "connect", flaky)
    monkeypatch.setattr(catalog.time, "sleep", sleeps.append)
    con = catalog.connect(poll_s=7)
    assert calls["n"] == 3 and sleeps == [7, 7]
    con.close()
    def always_locked(*a, **k):
        raise duckdb.IOException("locked")

    monkeypatch.setattr(catalog.duckdb, "connect", always_locked)
    with pytest.raises(duckdb.IOException):
        catalog.connect(max_wait_s=-1)                                        # deadline already passed


def test_analyze_report_includes_zotero_sections(cfg, tmp_path):
    con = catalog.connect()
    con.execute("create table if not exists zotero_tags (item_key varchar, tag varchar)")
    con.execute("create table if not exists zotero_collections (item_key varchar, collection_name varchar)")
    con.execute("insert into zotero_tags values ('K1','ml'), ('K2','ml'), ('K3','stats')")
    con.execute("insert into zotero_collections values ('K1','Thesis')")
    add("K1", "a.pdf")
    stats = analyze.collect_stats(con)
    text = analyze.render_report(stats, __import__("datetime").datetime(2026, 1, 2, 3, 4))
    assert "## Top Zotero tags" in text and "| ml | 2 |" in text
    assert "## Top Zotero collections" in text and "| Thesis | 1 |" in text
    assert "Generated 2026-01-02 03:04" in text
    con.close()


def test_cli_handlers_dispatch(cfg, monkeypatch, tmp_path):
    calls = []
    import dlt.discover as d
    import dlt.zotero_index as z
    monkeypatch.setattr(d, "scan", lambda fresh: calls.append(("scan", fresh)) or ([], [], []))
    monkeypatch.setattr(d, "insert_new", lambda con, found, dry_run: calls.append(("insert", dry_run)))
    monkeypatch.setattr(z, "discover_from_zotero", lambda con: calls.append("zd"))
    monkeypatch.setattr(z, "enrich_existing", lambda con: calls.append("zl"))
    results = iter([{"remaining": 3}, {"remaining": 0}])
    monkeypatch.setattr(convert, "run", lambda **kw: calls.append(("run", kw)) or next(results, None))
    monkeypatch.setattr(analyze, "generate", lambda con, out_path: calls.append(("gen", out_path)))
    for argv in (["discover", "--fresh", "--dry-run"], ["zotero-discover"], ["zotero-link"],
                 ["convert", "--max-docs", "5", "--loop"], ["status"], ["analyze", "--out", str(tmp_path / "r.md")]):
        monkeypatch.setattr(sys, "argv", ["dlt", *argv])
        cli.main()
    assert calls[:4] == [("scan", True), ("insert", True), "zd", "zl"]
    assert calls[4] == ("run", {"max_docs": 5}) and calls[5] == ("run", {"max_docs": 5})   # loop until remaining 0
    assert calls[6] == ("run", {"status_only": True})
    assert calls[7] == ("gen", tmp_path / "r.md")


def test_cli_convert_without_loop_runs_once_and_loop_stops_on_none(cfg, monkeypatch):
    runs = []
    monkeypatch.setattr(convert, "run", lambda **kw: runs.append(kw))
    monkeypatch.setattr(sys, "argv", ["dlt", "convert"])
    cli.main()
    assert runs == [{"max_docs": None}]
    runs.clear()
    monkeypatch.setattr(sys, "argv", ["dlt", "convert", "--loop"])
    cli.main()
    assert len(runs) == 1                                                     # None result ends the loop


def test_cli_analyze_default_out_path(cfg, monkeypatch):
    seen = []
    monkeypatch.setattr(analyze, "generate", lambda con, out_path: seen.append(out_path))
    monkeypatch.setattr(sys, "argv", ["dlt", "analyze"])
    cli.main()
    assert seen == [None]


# =============================== discover helpers ===============================

def test_discover_run_wrapper_and_listing_failures(monkeypatch):
    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    monkeypatch.setattr(subprocess, "run", timeout)
    assert discover._run(["rclone", "lsf", "x:"], 1) is None
    assert discover.rclone_lsd("x:") is None
    assert discover.rclone_lsf_recursive("x:", 1) is None
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: cp(1, ""))
    assert discover.rclone_lsd("x:") is None
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: cp(0, "a/\n\nb/\n"))
    assert discover.rclone_lsd("x:") == ["a", "b"]


def test_mtime_and_item_key():
    assert discover._mtime_ms("1970-01-02 00:00:00") == 86_400_000
    assert discover._mtime_ms("garbage") is None
    assert discover.item_key_for("a:", "p") == discover.item_key_for("a:", "p") != discover.item_key_for("b:", "p")
    assert discover.item_key_for("a:", "p").startswith("AUTO_") and len(discover.item_key_for("a:", "p")) == 17


def test_insert_new_dedupes_per_source_and_dry_run(cfg):
    con = catalog.connect()
    found = [("a:", "x/y.PDF", "1", "2024-01-01 00:00:00"), ("a:", "top.epub", "1", "bad"),
             ("b:", "x/y.PDF", "1", "2024-01-01 00:00:00")]
    logs = []
    assert discover.insert_new(con, found, dry_run=True, log=logs.append) == 3
    assert con.execute("select count(*) from documents").fetchone()[0] == 0
    assert discover.insert_new(con, found, log=logs.append) == 3
    assert discover.insert_new(con, found, log=logs.append) == 0               # all already catalogued
    rows = {(r[0], r[1]): r[2:] for r in con.execute(
        "select source, path, filetype, category, topic, mtime from documents").fetchall()}
    assert rows[("a:", "x/y.PDF")][:3] == ("pdf", "uncategorized", "x")        # lower-cased type, top folder topic
    assert rows[("a:", "top.epub")][2] == "(root)" or rows[("a:", "top.epub")][1] == "(root)"
    assert rows[("a:", "top.epub")][3] is None                                  # unparseable mtime
    assert discover.insert_new(con, [("c:", "q/r.txt", "1", "2024-01-01 00:00:00")],
                               categorize_fn=lambda p: ("cat", "top"), log=logs.append) == 1
    con.close()


def test_checkpoint_roundtrip_and_corrupt_lines(cfg, monkeypatch, tmp_path):
    cpf = tmp_path / "cp.jsonl"
    monkeypatch.setattr(discover, "CHECKPOINT_FILE", cpf)
    assert discover._load_checkpoint() == (set(), [], [], [])
    discover._checkpoint("r:", "f", "r:", [("p.pdf", "1", "t")], [{"a": 1}], [{"path": "x"}])
    with open(cpf, "a") as fh:
        fh.write("not json\n")
    done, found, archives, skipped = discover._load_checkpoint()
    assert done == {("r:", "f")} and found == [("r:", "p.pdf", "1", "t")]
    assert archives == [{"a": 1}] and skipped == [{"path": "x"}]


# =============================== mutation-driven additions ===============================

def test_markitdown_error_keeps_the_tail_of_stderr(monkeypatch, tmp_path):
    f = tmp_path / "x.mobi"
    f.write_bytes(b"x")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: cp(1, "", "a" * 100 + "b" * 500))
    assert convert.extract_text(f, "mobi") == (None, "b" * 500)


def test_extraction_error_keeps_the_tail_of_the_message(monkeypatch, tmp_path):
    def boom(path):
        raise ValueError("a" * 100 + "b" * 400)

    monkeypatch.setitem(convert.EXTRACTORS, "docx", boom)
    assert convert.extract_text(tmp_path / "x.docx", "docx") == (None, "docx extraction error: " + "b" * 400)


def test_soffice_default_filter_reads_txt_output(cfg, monkeypatch, tmp_path):
    f = tmp_path / "a.odt"
    f.write_text("x")

    def fake_run(cmd, **kw):
        outdir = Path(cmd[cmd.index("--outdir") + 1])
        (outdir / "A.txt").write_text("odt text")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert convert.convert_batch_soffice([("A", f, ".odt", "odt")])["A"] == ("odt text", None)


def test_bulk_upload_skips_rclone_for_empty_or_missing_staging_dirs(cfg, monkeypatch):
    (config.UPLOAD_DIR / "empty_").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or cp(0))
    assert convert.bulk_upload({"empty:", "missing:"}) == {"empty:": True, "missing:": True}
    assert calls == []


def test_title_and_extension_length_caps():
    assert len(convert.safe_title_for("t" * 100)) == 80 and convert.safe_title_for("t" * 80) == "t" * 80
    assert convert.ext_for("a." + "x" * 15) == "." + "x" * 10


def test_file_exactly_at_the_size_limit_is_converted(cfg, monkeypatch):
    import fitz
    fetch = config.FETCH_DIR / "dropbox_"
    fetch.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "hello")
    doc.save(str(fetch / "a.pdf"))
    doc.close()
    blob = (fetch / "a.pdf").read_bytes()
    monkeypatch.setattr(config, "MAX_SINGLE_FILE_MB", len(blob) / (1024 * 1024))     # exactly the limit
    add("K1", "a.pdf")

    def restage(items):
        (config.FETCH_DIR / "dropbox_").mkdir(parents=True, exist_ok=True)
        (config.FETCH_DIR / "dropbox_" / "a.pdf").write_bytes(blob)

    monkeypatch.setattr(convert, "bulk_fetch", restage)
    monkeypatch.setattr(convert, "bulk_upload", lambda sources: {s: True for s in sources})
    assert convert.run(log=lambda m: None) == {"converted": 1, "remaining": 0}
    assert statuses()["K1"] == ("ok", "ok")
    add("K2", "a.pdf")
    monkeypatch.setattr(config, "MAX_SINGLE_FILE_MB", len(blob) / (1024 * 1024) - 1e-6)
    convert.run(log=lambda m: None)
    assert statuses()["K2"][0] == "too_large"


def test_missing_upload_result_counts_as_a_failed_copy_back(cfg):
    con = catalog.connect()
    con.execute("insert into fulltext_md values (?,'T','dropbox:','p.pdf','/x.md','ok',5,1.0,now(),"
                "null,null,null,null)", ["K1"])
    counts = convert.BatchCounts()
    convert._apply_cloud_status(con, {"dropbox:"}, {}, counts)
    assert con.execute("select cloud_status from fulltext_md").fetchone()[0] == "failed"
    assert counts.cloud_failed == 1
    con.close()


def test_insert_new_stores_mtime_in_milliseconds(cfg):
    con = catalog.connect()
    discover.insert_new(con, [("a:", "x/y.pdf", "1", "2024-01-01 00:00:00")], log=lambda m: None)
    assert con.execute("select mtime from documents").fetchone()[0] == 1_704_067_200_000
    con.close()
