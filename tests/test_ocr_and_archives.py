"""Opt-in OCR and opt-in conversion of files catalogued inside archives."""
import io
import shutil
import zipfile

import fitz
import pytest

from dlt import archives, config, convert


def zip_bytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.mark.parametrize("path,parts", [
    ("a/b.zip!c/d.pdf", ["a/b.zip", "c/d.pdf"]),
    ("x.tar.gz!in.zip!d.pdf", ["x.tar.gz", "in.zip", "d.pdf"]),
    ("Wow!.pdf", None),
    ("a.zip!../../etc/passwd", None),
    ("a.zip!/abs.pdf", None),
])
def test_split_member_path(path, parts):
    assert archives.split_member_path(path) == parts


def test_materialize_reads_a_nested_member_and_reports_problems(tmp_path):
    outer = tmp_path / "a.zip"
    outer.write_bytes(zip_bytes({"in.zip": zip_bytes({"d.txt": "hello"})}))
    dest = tmp_path / "out" / "d.txt"
    assert archives.materialize(outer, ["in.zip", "d.txt"], dest, 10_000) is None
    assert dest.read_text() == "hello"
    assert "not found" in archives.materialize(outer, ["nope"], tmp_path / "n", 10_000)
    assert "size limit" in archives.materialize(outer, ["in.zip"], tmp_path / "s", 1)
    assert "fetched" in archives.materialize(tmp_path / "gone.zip", ["x"], tmp_path / "g", 1)


def test_enabled_option_downloads_the_archive_once(monkeypatch):
    items = {"r:": [("K1", "a.zip!1.pdf"), ("K2", "a.zip!2.pdf"), ("K3", "b.pdf")]}
    monkeypatch.setattr(config, "CONVERT_ARCHIVE_MEMBERS", True)
    assert [p for _k, p in convert._fetch_list(items)["r:"]] == ["a.zip", "b.pdf"]
    monkeypatch.setattr(config, "CONVERT_ARCHIVE_MEMBERS", False)
    assert convert._fetch_list(items) is items


def test_missing_ocr_program_is_an_error_not_a_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OCRMYPDF_BIN", str(tmp_path / "nope"))
    text, err = convert.ocr_pdf(tmp_path / "x.pdf")
    assert text is None and "could not run ocrmypdf" in err


@pytest.mark.skipif(not shutil.which("ocrmypdf"), reason="ocrmypdf not installed")
def test_ocr_reads_a_picture_only_pdf(tmp_path):
    source = fitz.open()
    page = source.new_page(width=595, height=842)
    for n, line in enumerate(["The quarterly report was approved by the board.",
                              "Revenue increased during the second half of the year."]):
        page.insert_text((50, 80 + 24 * n), line, fontsize=14)
    picture = page.get_pixmap(dpi=300)
    scanned = fitz.open()
    scanned.new_page(width=595, height=842).insert_image(fitz.Rect(0, 0, 595, 842), pixmap=picture)
    path = tmp_path / "scan.pdf"
    scanned.save(path)
    assert convert.extract_text(path, "pdf")[0].strip() == ""
    text, err = convert.ocr_pdf(path)
    assert err is None and "quarterly report was approved" in text.lower()


def test_run_converts_an_archive_member_end_to_end(isolated_config, monkeypatch):
    from dlt.catalog import connect

    monkeypatch.setattr(config, "CONVERT_ARCHIVE_MEMBERS", True)
    monkeypatch.setattr(config, "CLOUD_DEST_FOLDER", "")
    monkeypatch.setattr(config, "DISK_SAFETY_MARGIN_GB", 0)
    config.ensure_dirs()

    def fake_fetch(items_by_source):
        for source, items in items_by_source.items():
            assert [p for _k, p in items] == ["box/a.zip"]
            target = config.FETCH_DIR / convert._safe_dirname(source) / "box" / "a.zip"
            target.parent.mkdir(parents=True, exist_ok=True)
            doc = fitz.open()
            doc.new_page().insert_text((50, 80), "inside the archive")
            target.write_bytes(zip_bytes({"r.pdf": doc.tobytes()}))

    monkeypatch.setattr(convert, "bulk_fetch", fake_fetch)
    con = connect()
    for key, path in [("K1", "box/a.zip!r.pdf"), ("K2", "box/a.zip!missing.pdf")]:
        con.execute("insert into documents (item_key, title, path, source, filetype, is_duplicate) "
                    "values (?, ?, ?, 'r:', 'pdf', false)", [key, key, path])
    con.close()
    convert.run(log=lambda _line: None)
    con = connect()
    rows = dict(con.execute("select item_key, status from fulltext_md").fetchall())
    con.close()
    assert rows == {"K1": "ok", "K2": "archive_not_persisted"}
