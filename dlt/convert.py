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
from __future__ import annotations

import logging
import shutil
import subprocess
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Set, Tuple

import docx as python_docx
import fitz  # PyMuPDF
import openpyxl
import pptx as python_pptx

from . import config
from .catalog import connect, status_summary

logger = logging.getLogger(__name__)

# (text, error): exactly one of the two is None.
Extraction = Tuple[Optional[str], Optional[str]]
Log = Callable[[str], None]

fitz.TOOLS.mupdf_display_errors(False)
fitz.TOOLS.mupdf_display_warnings(False)
warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

SOFFICE_FILTER_BY_FILETYPE = {
    "doc": ("txt:Text", ".txt"),
    "ppt": ("txt:Text", ".txt"),
    "xls": ("csv:Text - txt - csv (StarCalc)", ".csv"),
}


# ── per-filetype extraction ──────────────────────────────────────────────

def extract_text_fitz(path) -> Extraction:
    """PDF/EPUB text via PyMuPDF; pages joined by blank lines."""
    doc = fitz.open(path)
    try:
        if doc.needs_pass:
            return None, "password-protected document"
        return "\n\n".join(page.get_text() for page in doc), None
    finally:
        doc.close()


def extract_text_docx(path) -> Extraction:
    """Paragraphs then tables (cells joined by " | ") from a .docx."""
    d = python_docx.Document(str(path))
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n\n".join(parts), None


def extract_text_pptx(path) -> Extraction:
    """One "## Slide N" block per slide containing text or table rows."""
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


def extract_text_xlsx(path) -> Extraction:
    """One "## Sheet: name" block per non-empty worksheet (cached values)."""
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


def extract_text_markitdown_fallback(path) -> Extraction:
    """Slow path: shell out to markitdown for filetypes with no in-process extractor."""
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


# Filetype -> in-process extractor. Add a new format by adding an entry here.
EXTRACTORS: Dict[str, Callable[..., Extraction]] = {
    "pdf": extract_text_fitz,
    "epub": extract_text_fitz,
    "docx": extract_text_docx,
    "pptx": extract_text_pptx,
    "xlsx": extract_text_xlsx,
}


def extract_text(path, filetype) -> Extraction:
    """Extract text from `path`; returns (text, None) or (None, error message)."""
    ft = (filetype or "").lower()
    extractor = EXTRACTORS.get(ft)
    if extractor is None:
        return extract_text_markitdown_fallback(path)
    try:
        return extractor(path)
    except Exception as e:  # noqa: BLE001 — any real-world file can be malformed
        return None, f"{ft} extraction error: {str(e)[-400:]}"


def convert_batch_soffice(items) -> Dict[str, Extraction]:
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
            logger.warning("soffice timed out after %ss; picking up any partial output", config.SOFFICE_TIMEOUT_S)
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

def bulk_fetch(items_by_source) -> None:
    """Stage every (item_key, path) for each source under config.FETCH_DIR (one rclone call per source)."""
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
            # partial fetch; the per-item staged.exists() check in run() handles it
            logger.warning("rclone fetch from %s timed out after %ss", source, config.RCLONE_TIMEOUT_S)


def bulk_upload(sources_present) -> Dict[str, bool]:
    """Copy each source's staged upload dir to its cloud destination; True per source on success."""
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
            logger.warning("rclone upload to %s timed out after %ss", source, config.RCLONE_TIMEOUT_S)
            results[source] = False
    return results


def _safe_dirname(source: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in source)


def safe_title_for(title: Optional[str]) -> str:
    """Filesystem-safe title (extension stripped, max 80 chars)."""
    title = title or "untitled"
    low = title.lower()
    for ext in config.DOCUMENT_EXTENSIONS:
        if low.endswith("." + ext):
            title = title[: -(len(ext) + 1)]
            break
    return "".join(c if c.isalnum() or c in " -_." else "_" for c in title)[:80]


def ext_for(catalog_path: str) -> str:
    """Dotted, sanitised extension of a catalog path (".bin" if none)."""
    suffix = Path(catalog_path).suffix.lstrip(".")
    safe = "".join(c for c in suffix if c.isalnum())[:10]
    return f".{safe}" if safe else ".bin"


def md_output_path(item_key: str, title: Optional[str]) -> Path:
    return config.MD_OUTPUT_DIR / f"{item_key}_{safe_title_for(title)}.md"


def _record(con, item_key, title, source, catalog_path, md_path, status, chars, source_mb, error,
            cloud_status=None, cloud_pdf_path=None, cloud_md_path=None):
    con.execute(
        "insert into fulltext_md values (?,?,?,?,?,?,?,?,now(),?,?,?,?) on conflict (item_key) do nothing",
        [item_key, title, source, catalog_path, md_path, status, chars, source_mb, error,
         cloud_status, cloud_pdf_path, cloud_md_path],
    )


def retry_cloud_failures(con, limit=200, log: Log = print) -> None:
    """Re-upload converted files whose cloud copy-back failed or never ran."""
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


PENDING_WHERE = "d.is_duplicate=false and f.item_key is null"


@dataclass
class BatchCounts:
    """Per-batch outcome tallies, reported in the final log line."""
    ok: int = 0
    fetch_failed: int = 0
    convert_failed: int = 0
    too_large: int = 0
    archive_gone: int = 0
    cloud_failed: int = 0


def _log_status(con, log: Log) -> dict:
    s = status_summary(con)
    log(f"catalog: {s['total']} documents, {s['done']} processed, {s['remaining']} remaining")
    for status, count in s["by_status"]:
        log(f"  {status}: {count}")
    return s


def _select_pending(con, max_docs: int):
    """Return (rows of up to max_docs pending documents, total pending count)."""
    pending = con.execute(f"""
        select d.item_key, d.title, d.path, d.source, d.filetype
        from documents d
        left join fulltext_md f on f.item_key = d.item_key
        where {PENDING_WHERE}
        order by d.item_key
        limit ?
    """, [max_docs]).fetchall()
    total = con.execute(f"""
        select count(*) from documents d
        left join fulltext_md f on f.item_key = d.item_key
        where {PENDING_WHERE}
    """).fetchone()[0]
    return pending, total


def _partition_pending(con, pending, counts: BatchCounts):
    """Record archive-internal paths as unconvertible; group the rest by source.

    Returns (to_fetch_by_source {source: [(item_key, path)]}, item_meta {item_key: (title, path, source, filetype)}).
    """
    to_fetch_by_source: Dict[str, list] = {}
    item_meta = {}
    for item_key, title, path, source, filetype in pending:
        item_meta[item_key] = (title, path, source, filetype)
        if "!" in path:
            _record(con, item_key, title, source, path, None, "archive_not_persisted", None, None,
                    "path is inside an archive not persisted at discovery time")
            counts.archive_gone += 1
            continue
        to_fetch_by_source.setdefault(source, []).append((item_key, path))
    return to_fetch_by_source, item_meta


def _finalize_ok(con, counts: BatchCounts, item_key, title, source, path, text, staged, upload_dir):
    """Write the .md, stage source + md for upload, and record success."""
    out_path = md_output_path(item_key, title)
    out_path.write_text(text)
    safe_title = safe_title_for(title)
    shutil.copy2(staged, upload_dir / f"{item_key}_{safe_title}{ext_for(path)}")
    shutil.copy2(out_path, upload_dir / f"{item_key}_{safe_title}.md")
    size_mb = staged.stat().st_size / (1024 * 1024)
    _record(con, item_key, title, source, path, str(out_path), "ok", len(text), round(size_mb, 2), None)
    counts.ok += 1


def _convert_fetched(con, to_fetch_by_source, item_meta, counts: BatchCounts):
    """Extract each fetched file in-process; queue legacy Office files for soffice.

    Returns (soffice_queue, sources_uploaded).
    """
    sources_uploaded: Set[str] = set()
    soffice_queue = []
    for source, items in to_fetch_by_source.items():
        upload_dir = config.UPLOAD_DIR / _safe_dirname(source)
        upload_dir.mkdir(parents=True, exist_ok=True)
        for item_key, path in items:
            title, _path, _source, filetype = item_meta[item_key]
            staged = config.FETCH_DIR / _safe_dirname(source) / path
            if not staged.exists():
                _record(con, item_key, title, source, path, None, "fetch_failed", None, None,
                        "not present after bulk rclone copy")
                counts.fetch_failed += 1
                continue
            size_mb = staged.stat().st_size / (1024 * 1024)
            if size_mb > config.MAX_SINGLE_FILE_MB:
                _record(con, item_key, title, source, path, None, "too_large", None, round(size_mb, 2),
                        f"{size_mb:.0f} MB exceeds MAX_SINGLE_FILE_MB={config.MAX_SINGLE_FILE_MB}")
                counts.too_large += 1
                continue
            ft = (filetype or "").lower()
            if ft in config.SOFFICE_FILETYPES:
                soffice_queue.append((item_key, staged, ext_for(path), source, upload_dir, ft))
                continue
            text, err = extract_text(staged, filetype)
            if err:
                _record(con, item_key, title, source, path, None, "convert_failed", None, round(size_mb, 2), err)
                counts.convert_failed += 1
                continue
            if not text.strip():
                _record(con, item_key, title, source, path, None, "empty_extraction", 0, round(size_mb, 2),
                        "no text extracted (likely scanned, no OCR)")
                counts.convert_failed += 1
                continue
            _finalize_ok(con, counts, item_key, title, source, path, text, staged, upload_dir)
            sources_uploaded.add(source)
    return soffice_queue, sources_uploaded


def _convert_soffice_queue(con, soffice_queue, item_meta, counts: BatchCounts, sources_uploaded: Set[str], log: Log):
    """Batch-convert queued legacy doc/ppt/xls files and record each outcome."""
    log(f"  batch-converting {len(soffice_queue)} legacy doc/ppt/xls file(s) via soffice...")
    soffice_results = convert_batch_soffice([(k, s, e, ft) for k, s, e, _s, _u, ft in soffice_queue])
    for item_key, staged, _ext, source, upload_dir, _ft in soffice_queue:
        title, path, _source, _filetype = item_meta[item_key]
        text, err = soffice_results.get(item_key, (None, "no result"))
        if err:
            _record(con, item_key, title, source, path, None, "convert_failed", None, None, err)
            counts.convert_failed += 1
        elif not text.strip():
            _record(con, item_key, title, source, path, None, "empty_extraction", 0, None, "no text extracted")
            counts.convert_failed += 1
        else:
            _finalize_ok(con, counts, item_key, title, source, path, text, staged, upload_dir)
            sources_uploaded.add(source)


def _apply_cloud_status(con, sources_uploaded: Set[str], upload_results: Dict[str, bool], counts: BatchCounts):
    """Mark freshly converted rows cloud_status ok/failed per their source's upload result."""
    for source in sources_uploaded:
        ok_upload = upload_results.get(source, False)
        rows = con.execute(
            "select item_key, title, catalog_path from fulltext_md "
            "where source=? and status='ok' and cloud_status is null",
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
                counts.cloud_failed += 1


def run(max_docs=None, status_only=False, log: Log = print):
    """Convert one capped batch of pending documents; return {"converted", "remaining"}
    (or the status summary dict when status_only)."""
    config.ensure_dirs()
    max_docs = max_docs or config.MAX_DOCS_PER_RUN
    con = connect()

    if status_only:
        return _log_status(con, log)

    free_gb = shutil.disk_usage(config.DATA_DIR).free / (1024 ** 3)
    if free_gb < config.DISK_SAFETY_MARGIN_GB:
        log(f"disk safety margin hit (<{config.DISK_SAFETY_MARGIN_GB} GB free); stopping.")
        con.close()
        return

    retry_cloud_failures(con, log=log)

    pending, total_pending = _select_pending(con, max_docs)
    log(f"→ {total_pending} document(s) pending. This batch: {len(pending)} docs.")

    shutil.rmtree(config.FETCH_DIR, ignore_errors=True)
    shutil.rmtree(config.UPLOAD_DIR, ignore_errors=True)

    counts = BatchCounts()
    to_fetch_by_source, item_meta = _partition_pending(con, pending, counts)

    log(f"  bulk-fetching {sum(len(v) for v in to_fetch_by_source.values())} file(s) "
        f"across {len(to_fetch_by_source)} source(s)...")
    bulk_fetch(to_fetch_by_source)

    soffice_queue, sources_uploaded = _convert_fetched(con, to_fetch_by_source, item_meta, counts)
    if soffice_queue:
        _convert_soffice_queue(con, soffice_queue, item_meta, counts, sources_uploaded, log)

    log(f"  bulk-uploading converted docs back to cloud ({len(sources_uploaded)} source(s))...")
    upload_results = bulk_upload(sources_uploaded)
    _apply_cloud_status(con, sources_uploaded, upload_results, counts)

    shutil.rmtree(config.FETCH_DIR, ignore_errors=True)
    shutil.rmtree(config.UPLOAD_DIR, ignore_errors=True)

    remaining = (total_pending - counts.ok - counts.archive_gone - counts.fetch_failed
                 - counts.convert_failed - counts.too_large)
    log(f"✓ batch complete: {counts.ok} converted ({counts.ok - counts.cloud_failed} cloud-copied), "
        f"{counts.archive_gone} archive-not-persisted, {counts.fetch_failed} fetch failed, "
        f"{counts.convert_failed} convert failed, {counts.too_large} too large. {remaining} still pending.")
    con.close()
    return {"converted": counts.ok, "remaining": remaining}
