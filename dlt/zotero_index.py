"""
Use a running Zotero desktop app as an indexing source: pull every item
that has a file attachment via Zotero's local HTTP API (no API key needed
— Settings -> Advanced -> "Allow other applications on this computer to
communicate with Zotero" must be on), and add it to the `documents` table
using the item's own Zotero key, tags, and collection membership.

This also works as a pure enrichment pass over documents discovered some
other way (discover.py's rclone sweep): if a document's item_key happens to
match a real Zotero item key, `enrich_existing()` backfills its tags/
collections without re-discovering anything.

Local-mode caveat: pyzotero's local API only exposes the CURRENTLY OPEN
Zotero library on this machine. If your document catalog was built from a
different Zotero library/session (a real, observed failure mode — a
snapshot taken elsewhere, or Zotero having been reset since), the item
keys simply won't overlap and `enrich_existing()` will link nothing. Check
`zot.count_items()` against your catalog's row count before assuming they
correspond to the same library.
"""
from pyzotero import zotero

from . import config


def _client():
    return zotero.Zotero(
        config.ZOTERO_LIBRARY_ID,
        config.ZOTERO_LIBRARY_TYPE,
        config.ZOTERO_API_KEY,
        local=config.ZOTERO_LOCAL,
    )


def ensure_zotero_tables(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS zotero_tags (
            item_key VARCHAR,
            tag VARCHAR,
            PRIMARY KEY (item_key, tag)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS zotero_collections (
            item_key VARCHAR,
            collection_key VARCHAR,
            collection_name VARCHAR,
            PRIMARY KEY (item_key, collection_key)
        )
    """)


def _collection_name_map(zot):
    return {c["key"]: c["data"]["name"] for c in zot.everything(zot.collections())}


def _attachment_path(zot, item_key):
    """Best-effort local file path for an item's primary attachment. Returns
    None for link-only or note-only items."""
    try:
        children = zot.children(item_key)
    except Exception:
        return None, None
    for child in children:
        data = child.get("data", {})
        if data.get("itemType") != "attachment":
            continue
        if data.get("linkMode") == "linked_file" and data.get("path"):
            return data["path"], data.get("filename") or data.get("title")
        if data.get("linkMode") in ("imported_file", "imported_url"):
            try:
                path = zot.file(child["key"])  # pyzotero resolves the local storage path
                return path, data.get("filename") or data.get("title")
            except Exception:
                continue
    return None, None


def discover_from_zotero(con, log=print, source_name="local:"):
    """Add every Zotero item with a real file attachment to `documents`,
    keyed by its actual Zotero item key. `source_name` is stored as the
    document's `source` — convert.py treats a source of "local:" as "read
    this path directly, no rclone fetch needed" (see convert.py's bulk_fetch)."""
    ensure_zotero_tables(con)
    zot = _client()
    collections = _collection_name_map(zot)
    existing = {r[0] for r in con.execute("select item_key from documents").fetchall()}

    items = zot.everything(zot.top())
    log(f"Zotero library has {len(items)} top-level item(s)")

    prepared, tag_rows, coll_rows = [], [], []
    for item in items:
        key = item["key"]
        data = item["data"]
        path, filename = _attachment_path(zot, key)
        if not path:
            continue  # no file attachment — nothing to convert
        for t in data.get("tags", []):
            tag_rows.append((key, t.get("tag", "")))
        for coll_key in data.get("collections", []):
            coll_rows.append((key, coll_key, collections.get(coll_key, coll_key)))
        if key in existing:
            continue
        filetype = (filename or path).rsplit(".", 1)[-1].lower() if "." in (filename or path) else "unknown"
        category = "zotero"
        topic = collections.get(data.get("collections", [None])[0], data.get("itemType", "uncategorized")) \
            if data.get("collections") else data.get("itemType", "uncategorized")
        prepared.append((key, data.get("title") or filename or key, path, source_name,
                          category, topic, filetype, data.get("date", "")[:4] or None,
                          None, False, None, None))

    if prepared:
        con.executemany("""
            insert into documents (item_key, title, path, source, category, topic,
                                    filetype, year, md5, is_duplicate, duplicate_of, mtime)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict (item_key) do nothing
        """, prepared)
    if tag_rows:
        con.executemany("insert into zotero_tags values (?, ?) on conflict do nothing", tag_rows)
    if coll_rows:
        con.executemany("insert into zotero_collections values (?, ?, ?) on conflict do nothing", coll_rows)

    log(f"added {len(prepared)} new document(s) from Zotero, "
        f"{len(tag_rows)} tag row(s), {len(coll_rows)} collection membership row(s)")
    return len(prepared)


def enrich_existing(con, log=print):
    """For documents discovered some other way, backfill zotero_key/tags/
    collections wherever item_key already matches a real Zotero key."""
    ensure_zotero_tables(con)
    zot = _client()
    collections = _collection_name_map(zot)
    items = zot.everything(zot.top())
    by_key = {i["key"]: i for i in items}

    catalog_keys = {r[0] for r in con.execute("select item_key from documents").fetchall()}
    overlap = catalog_keys & by_key.keys()
    log(f"{len(overlap)} of {len(catalog_keys)} catalogued item_key(s) match a real Zotero item")

    tag_rows, coll_rows = [], []
    for key in overlap:
        data = by_key[key]["data"]
        for t in data.get("tags", []):
            tag_rows.append((key, t.get("tag", "")))
        for coll_key in data.get("collections", []):
            coll_rows.append((key, coll_key, collections.get(coll_key, coll_key)))
        con.execute("update documents set zotero_key=? where item_key=?", [key, key])

    if tag_rows:
        con.executemany("insert into zotero_tags values (?, ?) on conflict do nothing", tag_rows)
    if coll_rows:
        con.executemany("insert into zotero_collections values (?, ?, ?) on conflict do nothing", coll_rows)
    log(f"enriched {len(overlap)} document(s) with {len(tag_rows)} tag row(s)")
    return len(overlap)
