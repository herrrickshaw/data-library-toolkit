"""Shared fixtures: an isolated config (temp data dir, no cloud copy-back)."""
from __future__ import annotations

import pytest

from dlt import config


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    """Point every config path at tmp_path and disable cloud copy-back."""
    data = tmp_path / "data"
    stage = data / "stage"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "CATALOG_DB", data / "catalog.duckdb")
    monkeypatch.setattr(config, "MD_OUTPUT_DIR", data / "md_corpus")
    monkeypatch.setattr(config, "STAGE_DIR", stage)
    monkeypatch.setattr(config, "FETCH_DIR", stage / "fetch")
    monkeypatch.setattr(config, "UPLOAD_DIR", stage / "upload")
    monkeypatch.setattr(config, "CLOUD_DEST_FOLDER", "")
    config.ensure_dirs()
    return config
