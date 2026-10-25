"""Tests for dlt/discover.py. rclone is replaced by a fake `_run` that answers
from an in-memory tree; no remote is ever contacted."""

import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dlt import catalog, config, discover  # noqa: E402


class FakeRclone:
    """Answers `rclone lsf` calls from a dict {remote_path: [(rel_path, size, mtime)]}.

    timeouts: set of targets whose *recursive* listing times out.
    """

    def __init__(self, files, timeouts=()):
        self.files = files            # "remote:folder/sub/file.pdf" -> (size, mtime)
        self.timeouts = set(timeouts)
        self.calls = []

    def __call__(self, cmd, timeout):
        self.calls.append(cmd)
        target = cmd[2]
        recursive = "-R" in cmd
        prefix = target if target.endswith(":") else target + "/"
        if recursive and target in self.timeouts:
            return None
        children = [k[len(prefix):] for k in self.files if k.startswith(prefix)]
        if "--dirs-only" in cmd:
            out = sorted({c.split("/")[0] + "/" for c in children if "/" in c})
        else:
            rows = [c for c in children if recursive or "/" not in c]
            out = [f"{c}\t{self.files[prefix + c][0]}\t{self.files[prefix + c][1]}" for c in sorted(rows)]
        return subprocess.CompletedProcess(cmd, 0, "\n".join(out) + ("\n" if out else ""), "")


MT = "2026-01-02 03:04:05"


@pytest.fixture
def tmpcfg(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CATALOG_DB", tmp_path / "c.duckdb")
    monkeypatch.setattr(discover, "CHECKPOINT_FILE", tmp_path / "ckpt.jsonl")
    monkeypatch.setattr(config, "SKIP_FOLDERS", set())
    return tmp_path


def _paths(found):
    return sorted(f[0] for f in found)


def test_extract_keeps_documents_and_archives():
    found, archives = [], []
    discover._extract([("a.pdf", "1", MT), ("b.PNG", "1", MT), ("c.zip", "1", MT), ("d.Docx", "2", MT)],
                      "top", found, archives, "r:")
    assert _paths(found) == ["top/a.pdf", "top/d.Docx"]
    assert archives == [{"path": "top/c.zip", "source": "r:"}]


def test_walk_lists_folder_recursively_when_fast(monkeypatch):
    fake = FakeRclone({"r:A/x.pdf": ("1", MT), "r:A/s/y.epub": ("2", MT)})
    monkeypatch.setattr(discover, "_run", fake)
    found, archives, skipped = [], [], []
    discover.walk("r:", "A", 0, found, archives, skipped)
    assert _paths(found) == ["A/s/y.epub", "A/x.pdf"]
    assert skipped == []


def test_walk_subdivides_timed_out_folder(monkeypatch):
    fake = FakeRclone({"r:A/s1/y.pdf": ("1", MT), "r:A/s2/z.pdf": ("1", MT)}, timeouts={"r:A"})
    monkeypatch.setattr(discover, "_run", fake)
    found, archives, skipped = [], [], []
    discover.walk("r:", "A", 0, found, archives, skipped)
    assert _paths(found) == ["A/s1/y.pdf", "A/s2/z.pdf"]


def test_walk_subdivision_keeps_loose_files_of_the_timed_out_folder(monkeypatch):
    """When a folder's recursive listing times out and it is split into subfolders,
    the files sitting directly in that folder must still be catalogued."""
    fake = FakeRclone({"r:A/loose.pdf": ("1", MT), "r:A/s1/y.pdf": ("1", MT)}, timeouts={"r:A"})
    monkeypatch.setattr(discover, "_run", fake)
    found, archives, skipped = [], [], []
    discover.walk("r:", "A", 0, found, archives, skipped)
    assert _paths(found) == ["A/loose.pdf", "A/s1/y.pdf"]


def test_walk_gives_up_at_max_depth(monkeypatch):
    monkeypatch.setattr(config, "DISCOVERY_MAX_DEPTH", 1)
    fake = FakeRclone({"r:A/s/t/x.pdf": ("1", MT)}, timeouts={"r:A", "r:A/s"})
    monkeypatch.setattr(discover, "_run", fake)
    found, archives, skipped = [], [], []
    discover.walk("r:", "A", 0, found, archives, skipped)
    assert found == [] and skipped[0]["path"] == "r:A/s"


def test_item_key_is_stable_and_source_specific():
    a = discover.item_key_for("dropbox:", "x/y.pdf")
    assert a == discover.item_key_for("dropbox:", "x/y.pdf")
    assert a != discover.item_key_for("gdrive:", "x/y.pdf")
    assert a.startswith("AUTO_") and len(a) == 17


def test_mtime_ms():
    assert discover._mtime_ms("1970-01-01 00:00:01.500000") == 1000
    assert discover._mtime_ms("garbage") is None


def test_scan_checkpoints_and_resumes(tmpcfg, monkeypatch):
    fake = FakeRclone({"r:root.pdf": ("1", MT), "r:A/a.pdf": ("1", MT), "r:B/b.pdf": ("1", MT)})
    monkeypatch.setattr(discover, "_run", fake)
    found, _, _ = discover.scan(["r:"], log=lambda *a: None)
    assert sorted(p for _s, p, _z, _m in found) == ["A/a.pdf", "B/b.pdf", "root.pdf"]
    n_calls = len(fake.calls)
    found2, _, _ = discover.scan(["r:"], log=lambda *a: None)
    assert sorted(found2) == sorted(found)
    # resumed run only re-lists the top level, it does not re-walk A or B
    assert len(fake.calls) - n_calls == 1


def test_insert_new_dedups_and_categorises(tmpcfg):
    con = catalog.connect()
    found = [("r:", "Top/a.pdf", "10", MT), ("r:", "b.docx", "5", MT)]
    assert discover.insert_new(con, found, log=lambda *a: None) == 2
    assert discover.insert_new(con, found, log=lambda *a: None) == 0
    rows = dict(con.execute("select path, topic from documents").fetchall())
    assert rows == {"Top/a.pdf": "Top", "b.docx": "(root)"}
    assert discover.insert_new(con, [("r:", "c.pdf", "1", MT)], dry_run=True, log=lambda *a: None) == 1
    assert con.execute("select count(*) from documents").fetchone()[0] == 2


def test_same_relative_path_on_a_second_remote_is_catalogued(tmpcfg):
    """DLT_RCLONE_REMOTES supports several remotes and item_key includes the source,
    so 'Reports/a.pdf' on gdrive: is a different document from the dropbox: one."""
    con = catalog.connect()
    discover.insert_new(con, [("dropbox:", "Reports/a.pdf", "1", MT)], log=lambda *a: None)
    n = discover.insert_new(con, [("gdrive:", "Reports/a.pdf", "1", MT)], log=lambda *a: None)
    assert n == 1
    assert sorted(r[0] for r in con.execute("select source from documents").fetchall()) == ["dropbox:", "gdrive:"]
