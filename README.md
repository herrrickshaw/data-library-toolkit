# data-library-toolkit

Turn a large, unsorted pile of documents across cloud storage (and/or a
Zotero library) into an easy-to-access, full-text-searchable markdown
dataset, tracked in a small DuckDB catalog.

Three stages, each independently runnable and resumable:

1. **Discover** — scan one or more rclone remotes (Dropbox, Google Drive,
   S3, ...) and/or a running Zotero library, and catalog every document
   found (pdf, docx, pptx, doc, ppt, xlsx, xls, epub, mobi, djvu, chm, ris,
   html) into a DuckDB table. Nothing is downloaded or converted at this
   stage — just discovered and recorded.
2. **Convert** — turn every catalogued, not-yet-converted document into a
   standalone `.md` file, batched for speed, and copy each result back to
   the cloud alongside its source (Create-only — never touches or deletes
   the original).
3. **Analyze** — join the catalog against conversion results and (if
   linked) Zotero tags/collections, and write a markdown report.

## Why this exists

Built from a real backfill: a personal Dropbox/Google Drive account with
years of accumulated documents, most never catalogued anywhere. The
architecture here reflects several hard-earned lessons from that process
(each documented at the point in the code where it matters):

- **A flat recursive listing of a whole cloud account can itself time out**
  before producing any output — `discover.py` uses recursive subdivision
  (list top-level, recurse into what's too big to list in one call) instead.
- **Batched I/O beats one-file-at-a-time by ~10x**: one `rclone copy
  --files-from=<list>` per batch instead of a `copyto` per file, in-process
  extraction (PyMuPDF/python-docx/python-pptx/openpyxl) instead of shelling
  out to a converter subprocess per file.
- **Legacy binary Office (.doc/.ppt/.xls) has no good pure-Python library**
  — these go through one batched `soffice --headless` call, with the export
  filter chosen per document type (a Writer filter against a Calc document
  silently produces nothing).
- **Every subprocess call with a timeout needs a try/except around it** —
  an uncaught `TimeoutExpired` crashes the whole batch; this cost real time
  repeatedly before every call site was audited.
- **A live-queried SQLite index shouldn't be cloud-mounted** — if you back
  up a resulting search index (e.g. via a tool like `qmd`) to cloud
  storage, snapshot it with `sqlite3 .backup` first and archive many small
  files into one compressed tarball before uploading, rather than syncing
  a folder of thousands of files (hits cloud API rate limits fast).

## Setup

```bash
pip install -r requirements.txt
rclone config          # set up whichever remotes you'll discover from
cp .env.example .env   # edit DLT_RCLONE_REMOTES, DLT_CLOUD_DEST_FOLDER, etc.
```

Optional, for full filetype coverage:
```bash
pipx install markitdown && pipx inject markitdown 'markitdown[pdf]'   # mobi/djvu/chm/ris/html fallback
brew install --cask libreoffice   # legacy .doc/.ppt/.xls
```

## Usage

```bash
python -m dlt.cli discover              # scan configured remotes, catalog new documents
python -m dlt.cli zotero-discover       # (optional) also pull from a running Zotero library
python -m dlt.cli zotero-link           # (optional) backfill tags/collections for matching items
python -m dlt.cli convert --loop        # convert everything pending, in capped batches
python -m dlt.cli status                # progress summary
python -m dlt.cli analyze               # write data/ANALYSIS.md
```

For a large backfill, run `convert` as a scheduled/backgrounded job rather
than in one sitting — it's fully resumable (checkpointed in the catalog
itself; killing it mid-batch costs nothing).

## Using Zotero as an index

If you run Zotero locally (Settings → Advanced → "Allow other applications
on this computer to communicate with Zotero"), `zotero-discover` adds every
item with a file attachment straight from your library — using Zotero's own
item key, tags, and collection membership as the catalog's `category`/
`topic`/`zotero_tags` fields. `zotero-link` does the reverse: for documents
already catalogued some other way, backfill Zotero metadata wherever the
item happens to share a real Zotero item key.

Caveat: this only sees the *currently open* Zotero library. If your catalog
was built from a different Zotero session or a snapshot taken elsewhere,
the item keys won't overlap — `zotero-link` will report near-zero matches
rather than silently linking the wrong things.

## Schema

Two tables in the DuckDB catalog (`data/catalog.duckdb` by default):

- `documents` — one row per discovered file: `item_key`, `title`, `path`
  (relative to its `source` remote), `source` (an rclone remote prefix, or
  `local:` for a Zotero-resolved local path), `category`, `topic`,
  `filetype`, `zotero_key`.
- `fulltext_md` — one row per conversion attempt: `status` (`ok` /
  `convert_failed` / `empty_extraction` / `fetch_failed` / ...), `chars`,
  `md_path`, `cloud_status`.

Plus, once Zotero-linked: `zotero_tags`, `zotero_collections`.

## License

Private tool for personal use. Add a license before making this public.
