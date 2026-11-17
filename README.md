# data-library-toolkit

Turns a large, unsorted pile of documents spread across cloud storage (any rclone remote) and/or a Zotero
library into a full-text-searchable markdown corpus, tracked in a small DuckDB catalog. It produces one `.md`
file per document (copied back next to the original in the cloud, create-only), a `documents` / `fulltext_md`
catalog at `data/catalog.duckdb`, and a markdown insights report at `data/ANALYSIS.md`.

> Status: maintained, personal tool published as-is. No licence file yet — all rights reserved. Built from a
> real Dropbox/Google Drive backfill; the failure modes it works around are documented in the code.

New to the repo? See [How to use this repo with AI](HOW_TO_USE_WITH_AI.md).

## What it does
- **Discover** documents (pdf, docx, pptx, doc, ppt, xlsx, xls, epub, mobi, djvu, chm, ris, html, htm) on one or more rclone remotes, with a resumable checkpoint file.
- **Pull from Zotero** (`zotero-discover`) using its local API, keeping Zotero item keys, tags and collections; `zotero-link` backfills them onto already-catalogued items.
- **Convert** pending documents to markdown in capped, resumable batches (bulk `rclone copy --files-from`, in-process extraction, one batched `soffice` call for legacy Office).
- **Copy back** each `.md` (and the source file) to `<remote><DLT_CLOUD_DEST_FOLDER>/` — create-only, never touches the original.
- **Analyze**: report by status, filetype, category, source, plus top Zotero tags/collections.

## Quick start
```bash
git clone https://github.com/herrrickshaw/data-library-toolkit.git
cd data-library-toolkit
python -m venv .venv && source .venv/bin/activate   # Python 3.10+ (pyzotero requires it)
pip install -r requirements.txt      # or: pip install -e .   (installs a `dlt` command)
rclone config                        # set up the remotes you will discover from
cp .env.example .env                 # then export/edit DLT_RCLONE_REMOTES etc.
python -m dlt.cli discover && python -m dlt.cli convert --loop
```
Optional, for full filetype coverage:
```bash
pipx install markitdown && pipx inject markitdown 'markitdown[pdf]'   # mobi/djvu/chm/ris/html fallback
brew install --cask libreoffice                                       # legacy .doc/.ppt/.xls
```
Other commands:
```bash
python -m dlt.cli zotero-discover   # (optional) add items from a running Zotero
python -m dlt.cli zotero-link       # (optional) backfill tags/collections for matching items
python -m dlt.cli status            # progress summary
python -m dlt.cli analyze [--out PATH]   # writes data/ANALYSIS.md
```
`convert` is fully resumable (checkpointed in the catalog); run it as a scheduled/background job for large backfills.

## How it works
| Stage | Module | Output |
|---|---|---|
| Discover | `dlt/discover.py` | rows in `documents`; `data/discover_checkpoint.jsonl` |
| Zotero index | `dlt/zotero_index.py` | `documents` rows with Zotero keys; `zotero_tags`, `zotero_collections` |
| Convert | `dlt/convert.py` | `data/md_corpus/<key>_<title>.md`; rows in `fulltext_md`; cloud copy-back |
| Analyze | `dlt/analyze.py` | `data/ANALYSIS.md` |
| Catalog / config / CLI | `dlt/catalog.py`, `dlt/config.py`, `dlt/cli.py` | schema, env-driven settings, sub-commands |

Design decisions (each earned from a real failure):
- **Recursive subdivision for discovery.** A flat recursive listing of a whole cloud account can itself time out; `discover.py` lists a folder, and if that times out, lists its subfolders and recurses (up to `DLT_DISCOVERY_MAX_DEPTH`).
- **Batched I/O.** One `rclone copy --files-from` per batch instead of a `copyto` per file (~10x faster); in-process extraction (PyMuPDF, python-docx, python-pptx, openpyxl) instead of a subprocess per file. Extractors live in the `EXTRACTORS` dict in `convert.py`.
- **Legacy .doc/.ppt/.xls** have no good pure-Python library: they go through one batched `soffice --headless` call, with the export filter chosen per type (a Writer filter on a Calc file silently yields nothing).
- **Every subprocess with a timeout handles `TimeoutExpired`** (now logged); an uncaught timeout used to crash a whole batch.
- **Copy-back status is tracked separately** (`cloud_status`), so a failed upload self-heals on the next run without re-converting.
- **Don't cloud-mount a live SQLite index.** If you back up a search index (e.g. via `qmd`), snapshot it with `sqlite3 .backup` and upload one compressed tarball rather than thousands of small files.
- **DuckDB single-writer:** `connect()` waits through a transient writer lock (up to 4 h) instead of crashing.

## Repository layout
| Path | Purpose |
|---|---|
| `dlt/` | the package (modules above) |
| `tests/` | pytest suite; `tests/golden/` holds outputs captured from the pre-refactor code |
| `.env.example` | every `DLT_*` setting with comments |
| `data/` | generated at runtime (catalog, markdown corpus, staging); gitignored |
| `HOW_TO_USE_WITH_AI.md`, `CLAUDE.md` | AI-assistant guidance |

## Schema
`data/catalog.duckdb`:
- `documents` — one row per file: `item_key`, `title`, `path` (relative to `source`), `source` (rclone prefix, or `local:` for a Zotero-resolved path), `category`, `topic`, `filetype`, `year`, `md5`, `is_duplicate`, `duplicate_of`, `mtime`, `date_added`, `zotero_key`.
- `fulltext_md` — one row per conversion attempt: `status` (`ok`, `convert_failed`, `empty_extraction`, `fetch_failed`, `too_large`, `archive_not_persisted`), `chars`, `md_path`, `cloud_status`, `cloud_pdf_path`, `cloud_md_path`, `error`.
- `zotero_tags`, `zotero_collections` once Zotero-linked.

## Data sources
| Source | What is taken | Access route | Caveat |
|---|---|---|---|
| rclone remotes (Dropbox, Drive, S3, ...) | file listings and the documents themselves | `rclone` CLI, your own `rclone config` | read-only on the source; copy-back is create-only |
| Zotero desktop (local API) | items with file attachments, tags, collections | `pyzotero`, local mode (Settings → Advanced → allow other applications) | only the *currently open* library is visible; with a mismatched library `zotero-link` reports near-zero matches |

## Configuration
Environment variables (see `.env.example`); no secrets are required or committed.
`DLT_DATA_DIR` (`./data`), `DLT_RCLONE_REMOTES` (`dropbox:`), `DLT_CLOUD_DEST_FOLDER` (`dlt_md_corpus`, empty disables copy-back), `DLT_SKIP_FOLDERS`, `DLT_ZOTERO_LOCAL` (`1`), `DLT_ZOTERO_LIBRARY_ID`, `DLT_ZOTERO_LIBRARY_TYPE`, `DLT_ZOTERO_API_KEY` (only for web-API mode — keep in your environment, never in git), `DLT_MAX_DOCS_PER_RUN` (1000), `DLT_MAX_SINGLE_FILE_MB` (300), `DLT_DISK_SAFETY_MARGIN_GB` (5). Further tuning keys (timeouts, rclone transfers, binary paths) are in `dlt/config.py`.
Note that `.env` is not auto-loaded; export the variables or use `set -a; source .env; set +a`.

## Testing
```bash
pip install pytest ruff
pytest -q          # 9 tests, no network, no rclone/soffice needed (fake rclone + fake Zotero client)
ruff check dlt tests
```

## Limitations and known issues
- Scanned PDFs are not OCR'd; they are recorded as `empty_extraction`.
- Files inside archives (path containing `!`) are recorded as `archive_not_persisted`.
- `analyze` report quirks kept for output stability: the "Processed" row lacks its closing `|`, and a blank line appears when no document converted.
- The batch log reports "cloud-copied" counts even when copy-back is disabled.
- Text extraction for non-fast-path types needs `markitdown` and `soffice` installed separately.

## Licence
No licence file yet — all rights reserved.
