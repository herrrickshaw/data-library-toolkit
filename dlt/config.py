"""
Central configuration, loaded from environment variables (with sensible
defaults) rather than hardcoded — this is the main thing that changes
between "a script for one person's Dropbox" and a reusable tool.

Copy .env.example to .env and edit it, or export these directly.
"""
import os
import shutil
from pathlib import Path

# ── Where things live ────────────────────────────────────────────────────
DATA_DIR = Path(os.environ.get("DLT_DATA_DIR", "./data")).resolve()
CATALOG_DB = Path(os.environ.get("DLT_CATALOG_DB", DATA_DIR / "catalog.duckdb"))
MD_OUTPUT_DIR = Path(os.environ.get("DLT_MD_OUTPUT_DIR", DATA_DIR / "md_corpus"))
STAGE_DIR = Path(os.environ.get("DLT_STAGE_DIR", DATA_DIR / "stage"))
FETCH_DIR = STAGE_DIR / "fetch"
UPLOAD_DIR = STAGE_DIR / "upload"

# ── rclone remotes to discover/fetch from ────────────────────────────────
# Comma-separated list of rclone remote prefixes, e.g. "dropbox:,gdrive:,s3:mybucket/".
# Each becomes both a discovery target and a `source` value stored per document
# — there is no separate name->remote mapping to keep in sync.
RCLONE_REMOTES = [r.strip() for r in os.environ.get("DLT_RCLONE_REMOTES", "dropbox:").split(",") if r.strip()]

# Where converted files get copied back to (Create-only — never touches or
# deletes the original source file). Set to "" to disable copy-back entirely.
CLOUD_DEST_FOLDER = os.environ.get("DLT_CLOUD_DEST_FOLDER", "dlt_md_corpus")

# Top-level folder names (case-insensitive) to skip entirely during a
# discovery sweep — e.g. huge machine-backup trees, or a tool's own
# per-item storage mirror that's pathologically slow to enumerate
# (thousands of near-empty subfolders). Comma-separated.
SKIP_FOLDERS = {f.strip().lower() for f in os.environ.get("DLT_SKIP_FOLDERS", "").split(",") if f.strip()}

# ── External binaries (resolved via PATH by default; override for a
# specific install, or when running under launchd/cron, which use a
# minimal PATH that may not include Homebrew or ~/.local/bin) ────────────
def _bin(env_var, name):
    return os.environ.get(env_var) or shutil.which(name) or name

RCLONE_BIN = _bin("DLT_RCLONE_BIN", "rclone")
MARKITDOWN_BIN = _bin("DLT_MARKITDOWN_BIN", "markitdown")
SOFFICE_BIN = _bin("DLT_SOFFICE_BIN", "soffice")

# ── Zotero (local API — Zotero desktop app must be running with
# Settings -> Advanced -> "Allow other applications on this computer to
# communicate with Zotero" enabled) ───────────────────────────────────────
ZOTERO_LOCAL = os.environ.get("DLT_ZOTERO_LOCAL", "1") not in ("0", "false", "False")
ZOTERO_LIBRARY_ID = os.environ.get("DLT_ZOTERO_LIBRARY_ID", "0")  # "0" is a placeholder pyzotero accepts in local mode
ZOTERO_LIBRARY_TYPE = os.environ.get("DLT_ZOTERO_LIBRARY_TYPE", "user")
ZOTERO_API_KEY = os.environ.get("DLT_ZOTERO_API_KEY")  # only needed for ZOTERO_LOCAL=0 (web API)

# ── Tuning ────────────────────────────────────────────────────────────────
MAX_DOCS_PER_RUN = int(os.environ.get("DLT_MAX_DOCS_PER_RUN", "1000"))
MAX_SINGLE_FILE_MB = float(os.environ.get("DLT_MAX_SINGLE_FILE_MB", "300"))
DISK_SAFETY_MARGIN_GB = float(os.environ.get("DLT_DISK_SAFETY_MARGIN_GB", "5"))
RCLONE_TIMEOUT_S = int(os.environ.get("DLT_RCLONE_TIMEOUT_S", "1800"))
RCLONE_TRANSFERS = os.environ.get("DLT_RCLONE_TRANSFERS", "16")
RCLONE_CHECKERS = os.environ.get("DLT_RCLONE_CHECKERS", "8")
MARKITDOWN_TIMEOUT_S = int(os.environ.get("DLT_MARKITDOWN_TIMEOUT_S", "180"))
SOFFICE_TIMEOUT_S = int(os.environ.get("DLT_SOFFICE_TIMEOUT_S", "900"))
DISCOVERY_PER_CALL_TIMEOUT_S = int(os.environ.get("DLT_DISCOVERY_TIMEOUT_S", "180"))
DISCOVERY_MAX_DEPTH = int(os.environ.get("DLT_DISCOVERY_MAX_DEPTH", "4"))

DOCUMENT_EXTENSIONS = {"pdf", "docx", "pptx", "doc", "ppt", "xlsx", "xls",
                        "epub", "mobi", "djvu", "chm", "ris", "html", "htm"}
FAST_PATH_FILETYPES = {"pdf", "epub", "docx", "pptx", "xlsx"}
SOFFICE_FILETYPES = {"doc", "ppt", "xls"}


def ensure_dirs():
    for d in (DATA_DIR, MD_OUTPUT_DIR, STAGE_DIR):
        d.mkdir(parents=True, exist_ok=True)
