"""Regressions for two reporting bugs previously listed under README limitations."""
from datetime import datetime

from dlt import analyze, config, convert


def test_disabled_copy_back_records_no_cloud_state(isolated_config, monkeypatch):
    monkeypatch.setattr(config, "CLOUD_DEST_FOLDER", "")
    monkeypatch.setattr(config, "DISK_SAFETY_MARGIN_GB", 0)
    monkeypatch.setattr(convert, "bulk_upload", lambda *a: (_ for _ in ()).throw(AssertionError("uploaded")))
    monkeypatch.setattr(convert, "bulk_fetch", lambda *a: None)
    config.ensure_dirs()
    logs = []
    convert.run(log=logs.append)
    assert any("(cloud copy-back disabled)" in line for line in logs)
    assert not any("cloud-copied" in line for line in logs)


def test_retry_is_a_no_op_when_copy_back_disabled(monkeypatch):
    monkeypatch.setattr(config, "CLOUD_DEST_FOLDER", "")
    convert.retry_cloud_failures(None)  # must return before touching the catalog


def test_report_overview_table_is_well_formed():
    stats = dict(total=4, done=2, ok=0, zotero_linked=0, avg_chars=None, near_empty=0,
                 by_status=[], by_filetype=[], by_category=[], by_source=[],
                 top_tags=[], top_collections=[])
    lines = analyze.render_report(stats, datetime(2026, 1, 1)).split("\n")
    table = lines[lines.index("| | |"):lines.index("## By conversion status") - 1]
    assert "| Processed | 2 (50.0%) |" in table
    assert all(row.startswith("|") and row.endswith("|") for row in table)
