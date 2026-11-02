"""Characterisation of zotero_index with a fake pyzotero client (no network)."""
from __future__ import annotations

import json
from pathlib import Path

from dlt import zotero_index
from dlt.catalog import connect

GOLDEN = Path(__file__).parent / "golden" / "zotero.json"


class FakeZot:
    def __init__(self):
        self._items = [
            {"key": "AAA", "data": {"title": "Paper A", "itemType": "journalArticle", "date": "2021-05-01",
                                    "tags": [{"tag": "ml"}, {"tag": "stats"}], "collections": ["C1"]}},
            {"key": "BBB", "data": {"title": "", "itemType": "book", "tags": [], "collections": []}},
            {"key": "CCC", "data": {"title": "No file", "itemType": "note", "tags": [], "collections": []}},
            {"key": "DDD", "data": {"title": "Broken", "itemType": "book", "tags": [], "collections": []}},
        ]

    def collections(self):
        return [{"key": "C1", "data": {"name": "Coll One"}}]

    def everything(self, x):
        return x

    def top(self):
        return self._items

    def children(self, key):
        if key == "AAA":
            return [{"key": "n1", "data": {"itemType": "note"}},
                    {"key": "AT1", "data": {"itemType": "attachment", "linkMode": "imported_file",
                                            "filename": "a.pdf"}}]
        if key == "BBB":
            return [{"key": "AT2", "data": {"itemType": "attachment", "linkMode": "linked_file",
                                            "path": "/x/b.DOCX", "filename": "b.DOCX"}}]
        if key == "DDD":
            raise RuntimeError("boom")
        return []

    def file(self, key):
        return "/zstore/" + key + "/a.pdf"


def test_zotero_discover_and_link(isolated_config, monkeypatch):
    monkeypatch.setattr(zotero_index, "_client", lambda: FakeZot())
    con = connect()
    logs: list = []
    n = zotero_index.discover_from_zotero(con, log=logs.append)
    con.execute("insert into documents (item_key,title,path,source,category,topic,filetype) "
                "values ('CCC','x','p','dropbox:','c','t','pdf')")
    linked = zotero_index.enrich_existing(con, log=logs.append)
    snap = {
        "n": n, "linked": linked, "logs": logs,
        "docs": [list(r) for r in con.execute(
            "select item_key,title,path,source,category,topic,filetype,year,zotero_key "
            "from documents order by item_key").fetchall()],
        "tags": [list(r) for r in con.execute("select * from zotero_tags order by 1,2").fetchall()],
        "colls": [list(r) for r in con.execute("select * from zotero_collections order by 1,2").fetchall()],
    }
    con.close()
    if not GOLDEN.exists():
        GOLDEN.write_text(json.dumps(snap, indent=1, sort_keys=True))
    assert snap == json.loads(GOLDEN.read_text())
