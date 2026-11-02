"""CLI smoke tests: parser shape and sub-command dispatch table."""
from __future__ import annotations

import pytest

from dlt import cli


def test_every_subcommand_has_a_handler():
    parser = cli.build_parser()
    choices = next(a for a in parser._actions if a.dest == "command").choices
    assert set(choices) == set(cli.COMMANDS)


def test_parser_flags():
    p = cli.build_parser()
    a = p.parse_args(["convert", "--max-docs", "5", "--loop"])
    assert (a.command, a.max_docs, a.loop) == ("convert", 5, True)
    a = p.parse_args(["discover", "--fresh", "--dry-run"])
    assert a.fresh and a.dry_run
    assert p.parse_args(["analyze", "--out", "x.md"]).out == "x.md"


def test_unknown_command_exits():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["nope"])


def test_status_runs_on_empty_catalog(isolated_config, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["dlt", "status"])
    cli.main()
    assert "catalog: 0 documents" in capsys.readouterr().out
