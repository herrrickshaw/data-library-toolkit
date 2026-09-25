"""
DuckDB schema for the two core tables (`documents`, `fulltext_md`) plus a
`connect()` that retries through a transient writer lock — DuckDB is
single-writer, so a discovery/analyze run started while a conversion batch
is mid-flight should wait its turn rather than crash.
"""
import time

import duckdb

from . import config


def connect(read_only=False, max_wait_s=4 * 3600, poll_s=15):
    deadline = time.time() + max_wait_s
    attempt = 0
    while True:
        try:
            con = duckdb.connect(str(config.CATALOG_DB), read_only=read_only)
            init_schema(con)
            return con
        except duckdb.IOException:
            attempt += 1
            if time.time() > deadline:
                raise
            time.sleep(poll_s)


def init_schema(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            item_key VARCHAR PRIMARY KEY,
            title VARCHAR,
            path VARCHAR,          -- path relative to its `source` remote root
            source VARCHAR,        -- an rclone remote prefix, e.g. "dropbox:"
            category VARCHAR,
            topic VARCHAR,
            filetype VARCHAR,
            year VARCHAR,
            md5 VARCHAR,
            is_duplicate BOOLEAN DEFAULT false,
            duplicate_of VARCHAR,
            mtime BIGINT,           -- ms since epoch
            date_added TIMESTAMP DEFAULT current_timestamp,
            zotero_key VARCHAR      -- set by zotero_index.py once linked
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS fulltext_md (
            item_key VARCHAR PRIMARY KEY,
            title VARCHAR,
            source VARCHAR,
            catalog_path VARCHAR,
            md_path VARCHAR,
            status VARCHAR,        -- ok | archive_not_persisted | fetch_failed | convert_failed
                                    -- | empty_extraction | too_large | unsupported_source
            chars BIGINT,
            source_mb DOUBLE,
            converted_at TIMESTAMP,
            error VARCHAR,
            cloud_status VARCHAR,  -- ok | failed (only meaningful when status='ok')
            cloud_pdf_path VARCHAR,
            cloud_md_path VARCHAR
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_documents_filetype ON documents(filetype)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_documents_category ON documents(category)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_fulltext_md_status ON fulltext_md(status)")


def status_summary(con):
    total = con.execute("select count(*) from documents where is_duplicate=false").fetchone()[0]
    done = con.execute("select count(*) from fulltext_md").fetchone()[0]
    by_status = con.execute("select status, count(*) from fulltext_md group by 1 order by 2 desc").fetchall()
    return {"total": total, "done": done, "remaining": total - done, "by_status": by_status}
