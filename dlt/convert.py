"""
Convert every catalogued, not-yet-converted document to a standalone .md
file, batched, and copy each conversion back to the cloud alongside its
source.

Design notes (each one earned from a real failure, not theoretical):

1. Batched I/O, not one-file-at-a-time: one `rclone copy --files-from=<list>`
   per source to bulk-fetch a whole batch, then one bulk `rclone copy` per
   source to upload the whole batch back — not one `copyto` per file. This
   is the difference between a 300-doc batch taking ~50min vs ~5min.
2. Extraction is in-process (PyMuPDF/python-docx/python-pptx/openpyxl), not
   a markitdown subprocess per file: ~0.04-0.4s/doc vs ~4-5s/doc, because a
   subprocess pays a fresh interpreter-startup cost every single call.
   markitdown is kept only as the slow-path fallback for filetypes with no
   good in-process library (mobi, djvu, chm, ris, html, ...).
3. Legacy binary Office (.doc/.ppt/.xls) has NO markitdown converter at all
   (confirmed: UnsupportedFormatException) — these go through one batched
   `soffice --headless --convert-to` call instead, with the export filter
   chosen per document type (Writer's txt filter silently produces empty
   output on a Calc document and vice versa — this cost a full failed batch
   before the filter-per-filetype dispatch below existed).
4. Every `subprocess.run(..., timeout=...)` call is wrapped in
   try/except TimeoutExpired. An uncaught timeout crashes the whole batch;
   this class of bug alone cost 30 minutes per occurrence, repeatedly,
   before every call site was audited and fixed.
5. Cloud copy-back is Create-only (rclone copyto/copy to a new destination
   folder) and tracked separately from conversion status (`cloud_status`),
   so a failed copy-back self-heals on the next run without re-converting.
"""
import shutil
import subprocess
import warnings
from pathlib import Path

import docx as python_docx
import fitz  # PyMuPDF
import openpyxl
import pptx as python_pptx

from . import config
from .catalog import connect

fitz.TOOLS.mupdf_display_errors(False)
fitz.TOOLS.mupdf_display_warnings(False)
warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

SOFFICE_FILTER_BY_FILETYPE = {
    "doc": ("txt:Text", ".txt"),
    "ppt": ("txt:Text", ".txt"),
    "xls": ("csv:Text - txt - csv (StarCalc)", ".csv"),
}


# ── per-filetype extraction ──────────────────────────────────────────────

def extract_text_fitz(path):
    doc = fitz.open(path)
    try:
        if doc.needs_pass:
            return None, "password-protected document"
        return "\n\n".join(page.get_text() for page in doc), None
    finally:
        doc.close()


def extract_text_docx(path):
    d = python_docx.Document(str(path))
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n\n".join(parts), None


def extract_text_pptx(path):
    prs = python_pptx.Presentation(str(path))
    parts = []
    for i, slide in enumerate(prs.slides, 1):
        lines = [f"## Slide {i}"]
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    line = "".join(run.text for run in para.runs)
                    if line.strip():
                        lines.append(line)
            if shape.has_table:
                for row in shape.table.rows:
                    lines.append(" | ".join(cell.text for cell in row.cells))
        if len(lines) > 1:
            parts.append("\n".join(lines))
    return "\n\n".join(parts), None


def extract_text_xlsx(path):
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        parts = []
        for ws in wb.worksheets:
            lines = [f"## Sheet: {ws.title}"]
            for row in ws.iter_rows(values_only=True):
                if any(c is not None for c in row):
                    lines.append(" | ".join("" if c is None else str(c) for c in row))
            if len(lines) > 1:
                parts.append("\n".join(lines))
        return "\n\n".join(parts), None
    finally:
        wb.close()


def extract_text_markitdown_fallback(path):
    try:
        result = subprocess.run(
            [config.MARKITDOWN_BIN, str(path)],
            capture_output=True, text=True, timeout=config.MARKITDOWN_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return None, f"markitdown timeout after {config.MARKITDOWN_TIMEOUT_S}s"
    if result.returncode != 0:
        return None, (result.stderr or "markitdown failed").strip()[-500:]
    return result.stdout, None


def extract_text(path, filetype):
    ft = (filetype or "").lower()
    try:
        if ft in ("pdf", "epub"):
            return extract_text_fitz(path)
        if ft == "docx":
            return extract_text_docx(path)
        if ft == "pptx":
            return extract_text_pptx(path)
        if ft == "xlsx":
            return extract_text_xlsx(path)
    except Exception as e:  # noqa: BLE001 — any real-world file can be malformed
        return None, f"{ft} extraction error: {str(e)[-400:]}"
    return extract_text_markitdown_fallback(path)


def convert_batch_soffice(items):
    """items: list of (item_key, staged_path, ext, filetype). One `soffice
    --convert-to` call PER FILTER GROUP (doc/ppt share one, xls needs
    another — see module docstring point 3)."""
    if not items:
        return {}
    results = {}
    by_filter = {}
    for item_key, staged_path, ext, filetype in items:
        soffice_filter, out_ext = SOFFICE_FILTER_BY_FILETYPE.get(filetype, ("txt:Text", ".txt"))
        by_filter.setdefault(soffice_filter, []).append((item_key, staged_path, ext, out_ext))

    for soffice_filter, group in by_filter.items():
        batch_in = config.STAGE_DIR / "soffice_in"
        batch_out = config.STAGE_DIR / "soffice_out"
        profile_dir = config.STAGE_DIR / "soffice_profile"
        batch_in.mkdir(parents=True, exist_ok=True)
        batch_out.mkdir(parents=True, exist_ok=True)
        input_paths = []
        for item_key, staged_path, ext, _out_ext in group:
            dest = batch_in / f"{item_key}{ext}"
            shutil.copy2(staged_path, dest)
            input_paths.append(str(dest))
        try:
            subprocess.run(
                [config.SOFFICE_BIN, "--headless", "--norestore",
                 f"-env:UserInstallation=file://{profile_dir}",
                 "--convert-to", soffice_filter, "--outdir", str(batch_out)] + input_paths,
                capture_output=True, text=True, timeout=config.SOFFICE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            pass  # partial output, if any, is still picked up below
        for item_key, _staged_path, _ext, out_ext in group:
            out_file = batch_out / f"{item_key}{out_ext}"
            if out_file.exists():
                results[item_key] = (out_file.read_text(errors="replace"), None)
            else:
                results[item_key] = (None, "soffice produced no output for this file")
        shutil.rmtree(batch_in, ignore_errors=True)
        shutil.rmtree(batch_out, ignore_errors=True)
        shutil.rmtree(profile_dir, ignore_errors=True)
    return results


# ── rclone batch I/O ──────────────────────────────────────────────────────

def bulk_fetch(items_by_source):
    for source, items in items_by_source.items():
        if not items:
            continue
        dest = config.FETCH_DIR / _safe_dirname(source)
        dest.mkdir(parents=True, exist_ok=True)

        if source == "local:":
            # Zotero-discovered items store a real local filesystem path
            # directly (see zotero_index.py) — no rclone remote involved,
            # just copy each one into the same per-batch staging layout
            # everything else uses.
            for _key, path in items:
                src = Path(path)
                if src.exists():
                    (dest / path.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest / path.lstrip("/"))
            continue

        listfile = config.STAGE_DIR / f"fetch_list_{_safe_dirname(source)}.txt"
        listfile.write_text("\n".join(path for _key, path in items))
        try:
            subprocess.run(
                [config.RCLONE_BIN, "copy", source, str(dest),
                 "--files-from", str(listfile),
                 "--transfers", config.RCLONE_TRANSFERS, "--checkers", config.RCLONE_CHECKERS,
                 "--fast-list", "--ignore-errors"],
                capture_output=True, text=True, timeout=config.RCLONE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            pass  # partial fetch; per-item staged.exists() check below handles it


def bulk_upload(sources_present):
    results = {}
    for source in sources_present:
        local_dir = config.UPLOAD_DIR / _safe_dirname(source)
        if not config.CLOUD_DEST_FOLDER or not local_dir.exists() or not any(local_dir.iterdir()):
            results[source] = True
            continue
        try:
            result = subprocess.run(
                [config.RCLONE_BIN, "copy", str(local_dir), f"{source}{config.CLOUD_DEST_FOLDER}/",
                 "--transfers", config.RCLONE_TRANSFERS, "--checkers", config.RCLONE_CHECKERS],
                capture_output=True, text=True, timeout=config.RCLONE_TIMEOUT_S,
            )
            results[source] = result.returncode == 0
        except subprocess.TimeoutExpired:
            results[source] = False
    return results


def _safe_dirname(source):
    return "".join(c if c.isalnum() else "_" for c in source)


def safe_title_for(title):
    title = title or "untitled"
    low = title.lower()
    for ext in config.DOCUMENT_EXTENSIONS:
        if low.endswith("." + ext):
            title = title[: -(len(ext) + 1)]
            break
    return "".join(c if c.isalnum() or c in " -_." else "_" for c in title)[:80]


def ext_for(catalog_path):
    suffix = Path(catalog_path).suffix.lstrip(".")
    safe = "".join(c for c in suffix if c.isalnum())[:10]
    return f".{safe}" if safe else ".bin"


def md_output_path(item_key, title):
    return config.MD_OUTPUT_DIR / f"{item_key}_{safe_title_for(title)}.md"


def _record(con, item_key, title, source, catalog_path, md_path, status, chars, source_mb, error,
            cloud_status=None, cloud_pdf_path=None, cloud_md_path=None):
    con.execute(
        "insert into fulltext_md values (?,?,?,?,?,?,?,?,now(),?,?,?,?) on conflict (item_key) do nothing",
        [item_key, title, source, catalog_path, md_path, status, chars, source_mb, error,
         cloud_status, cloud_pdf_path, cloud_md_path],
    )


def retry_cloud_failures(con, limit=200, log=print):
    rows = con.execute("""
        select item_key, source, md_path, title
        from fulltext_md where status='ok' and (cloud_status='failed' or cloud_status is null)
        limit ?
    """, [limit]).fetchall()
    if not rows:
        return
    log(f"  retrying cloud copy-back for {len(rows)} item(s)...")
    for source in set(r[1] for r in rows):
        (config.UPLOAD_DIR / _safe_dirname(source)).mkdir(parents=True, exist_ok=True)
    for item_key, source, md_path, title in rows:
        if md_path and Path(md_path).exists():
            safe_title = safe_title_for(title)
            shutil.copy2(md_path, config.UPLOAD_DIR / _safe_dirname(source) / f"{item_key}_{safe_title}.md")
    upload_results = bulk_upload(set(r[1] for r in rows))
    healed = 0
    for item_key, source, md_path, title in rows:
        if not md_path or not Path(md_path).exists():
            continue
        if upload_results.get(source):
            safe_title = safe_title_for(title)
            con.execute("update fulltext_md set cloud_status='ok', cloud_md_path=? where item_key=?",
                        [f"{source}{config.CLOUD_DEST_FOLDER}/{item_key}_{safe_title}.md", item_key])
            healed += 1
    for source in set(r[1] for r in rows):
        shutil.rmtree(config.UPLOAD_DIR / _safe_dirname(source), ignore_errors=True)
    log(f"  ...{healed}/{len(rows)} healed.")


def run(max_docs=None, status_only=False, log=print):
    config.ensure_dirs()
    max_docs = max_docs or config.MAX_DOCS_PER_RUN
    con = connect()

    if status_only:
        from .catalog import status_summary
        s = status_summary(con)
        log(f"catalog: {s['total']} documents, {s['done']} processed, {s['remaining']} remaining")
        for status, count in s["by_status"]:
            log(f"  {status}: {count}")
        return s

    import shutil as _sh
    free_gb = _sh.disk_usage(config.DATA_DIR).free / (1024 ** 3)
    if free_gb < config.DISK_SAFETY_MARGIN_GB:
        log(f"disk safety margin hit (<{config.DISK_SAFETY_MARGIN_GB} GB free); stopping.")
        con.close()
        return

    retry_cloud_failures(con, log=log)

    pending = con.execute("""
        select d.item_key, d.title, d.path, d.source, d.filetype
        from documents d
        left join fulltext_md f on f.item_key = d.item_key
        where d.is_duplicate=false and f.item_key is null
        order by d.item_key
        limit ?
    """, [max_docs]).fetchall()
    total_pending = con.execute("""
        select count(*) from documents d
        left join fulltext_md f on f.item_key = d.item_key
        where d.is_duplicate=false and f.item_key is null
    """).fetchone()[0]
    log(f"→ {total_pending} document(s) pending. This batch: {len(pending)} docs.")

    _sh.rmtree(config.FETCH_DIR, ignore_errors=True)
    _sh.rmtree(config.UPLOAD_DIR, ignore_errors=True)

    n_archive_gone = n_unsupported = 0
    to_fetch_by_source, item_meta = {}, {}
    for item_key, title, path, source, filetype in pending:
        item_meta[item_key] = (title, path, source, filetype)
        if "!" in path:
            _record(con, item_key, title, source, path, None, "archive_not_persisted", None, None,
                    "path is inside an archive not persisted at discovery time")
            n_archive_gone += 1
            continue
        to_fetch_by_source.setdefault(source, []).append((item_key, path))

    log(f"  bulk-fetching {sum(len(v) for v in to_fetch_by_source.values())} file(s) "
        f"across {len(to_fetch_by_source)} source(s)...")
    bulk_fetch(to_fetch_by_source)

    n_ok = n_fetch_failed = n_convert_failed = n_too_large = n_cloud_failed = 0
    sources_uploaded = set()
    soffice_queue = []

    def finalize_ok(item_key, title, source, path, text, staged, upload_dir):
        nonlocal n_ok
        out_path = md_output_path(item_key, title)
        out_path.write_text(text)
        safe_title = safe_title_for(title)
        shutil.copy2(staged, upload_dir / f"{item_key}_{safe_title}{ext_for(path)}")
        shutil.copy2(out_path, upload_dir / f"{item_key}_{safe_title}.md")
        sources_uploaded.add(source)
        size_mb = staged.stat().st_size / (1024 * 1024)
        _record(con, item_key, title, source, path, str(out_path), "ok", len(text), round(size_mb, 2), None)
        n_ok += 1

    for source, items in to_fetch_by_source.items():
        upload_dir = config.UPLOAD_DIR / _safe_dirname(source)
        upload_dir.mkdir(parents=True, exist_ok=True)
        for item_key, path in items:
            title, _path, _source, filetype = item_meta[item_key]
            staged = config.FETCH_DIR / _safe_dirname(source) / path
            if not staged.exists():
                _record(con, item_key, title, source, path, None, "fetch_failed", None, None,
                        "not present after bulk rclone copy")
                n_fetch_failed += 1
                continue
            size_mb = staged.stat().st_size / (1024 * 1024)
            if size_mb > config.MAX_SINGLE_FILE_MB:
                _record(con, item_key, title, source, path, None, "too_large", None, round(size_mb, 2),
                        f"{size_mb:.0f} MB exceeds MAX_SINGLE_FILE_MB={config.MAX_SINGLE_FILE_MB}")
                n_too_large += 1
                continue
            if (filetype or "").lower() in config.SOFFICE_FILETYPES:
                soffice_queue.append((item_key, staged, ext_for(path), source, upload_dir, (filetype or "").lower()))
                continue
            text, err = extract_text(staged, filetype)
            if err:
                _record(con, item_key, title, source, path, None, "convert_failed", None, round(size_mb, 2), err)
                n_convert_failed += 1
                continue
            if not text.strip():
                _record(con, item_key, title, source, path, None, "empty_extraction", 0, round(size_mb, 2),
                        "no text extracted (likely scanned, no OCR)")
                n_convert_failed += 1
                continue
            finalize_ok(item_key, title, source, path, text, staged, upload_dir)

    if soffice_queue:
        log(f"  batch-converting {len(soffice_queue)} legacy doc/ppt/xls file(s) via soffice...")
        soffice_results = convert_batch_soffice([(k, s, e, ft) for k, s, e, _s, _u, ft in soffice_queue])
        for item_key, staged, _ext, source, upload_dir, _ft in soffice_queue:
            title, path, _source, _filetype = item_meta[item_key]
            text, err = soffice_results.get(item_key, (None, "no result"))
            if err:
                _record(con, item_key, title, source, path, None, "convert_failed", None, None, err)
                n_convert_failed += 1
            elif not text.strip():
                _record(con, item_key, title, source, path, None, "empty_extraction", 0, None, "no text extracted")
                n_convert_failed += 1
            else:
                finalize_ok(item_key, title, source, path, text, staged, upload_dir)

    log(f"  bulk-uploading converted docs back to cloud ({len(sources_uploaded)} source(s))...")
    upload_results = bulk_upload(sources_uploaded)
    for source in sources_uploaded:
        ok_upload = upload_results.get(source, False)
        rows = con.execute(
            "select item_key, title, catalog_path from fulltext_md where source=? and status='ok' and cloud_status is null",
            [source],
        ).fetchall()
        for item_key, title, catalog_path in rows:
            safe_title = safe_title_for(title)
            if ok_upload:
                con.execute(
                    "update fulltext_md set cloud_status='ok', cloud_pdf_path=?, cloud_md_path=? where item_key=?",
                    [f"{source}{config.CLOUD_DEST_FOLDER}/{item_key}_{safe_title}{ext_for(catalog_path)}",
                     f"{source}{config.CLOUD_DEST_FOLDER}/{item_key}_{safe_title}.md", item_key],
                )
            else:
                con.execute("update fulltext_md set cloud_status='failed' where item_key=?", [item_key])
                n_cloud_failed += 1

    shutil.rmtree(config.FETCH_DIR, ignore_errors=True)
    shutil.rmtree(config.UPLOAD_DIR, ignore_errors=True)

    remaining = total_pending - n_ok - n_archive_gone - n_unsupported - n_fetch_failed - n_convert_failed - n_too_large
    log(f"✓ batch complete: {n_ok} converted ({n_ok - n_cloud_failed} cloud-copied), "
        f"{n_archive_gone} archive-not-persisted, {n_fetch_failed} fetch failed, "
        f"{n_convert_failed} convert failed, {n_too_large} too large. {remaining} still pending.")
    con.close()
    return {"converted": n_ok, "remaining": remaining}
