"""
Generate a markdown report summarizing the catalog + conversion results +
(if linked) Zotero tags/collections — the "insights" layer over the raw
tables, meant to be read by a person, not queried.

`collect_stats` does the SQL, `render_report` is a pure function from stats to
markdown lines, `generate` ties them together and writes the file.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import config
from .zotero_index import ensure_zotero_tables

TOP_N = 20


def collect_stats(con) -> Dict[str, object]:
    """Run the report queries; returns the raw numbers and (label, count) row lists."""
    return {
        "total": con.execute("select count(*) from documents where is_duplicate=false").fetchone()[0],
        "done": con.execute("select count(*) from fulltext_md").fetchone()[0],
        "ok": con.execute("select count(*) from fulltext_md where status='ok'").fetchone()[0],
        "by_status": con.execute(
            "select status, count(*) from fulltext_md group by 1 order by 2 desc").fetchall(),
        "by_filetype": con.execute(f"""
            select d.filetype, count(*), sum(case when f.status='ok' then 1 else 0 end)
            from documents d left join fulltext_md f on f.item_key=d.item_key
            where d.is_duplicate=false group by 1 order by 2 desc limit {TOP_N}
        """).fetchall(),
        "by_category": con.execute("""
            select category, count(*) from documents where is_duplicate=false group by 1 order by 2 desc
        """).fetchall(),
        "by_source": con.execute("""
            select source, count(*) from documents where is_duplicate=false group by 1 order by 2 desc
        """).fetchall(),
        "avg_chars": con.execute("select avg(chars) from fulltext_md where status='ok'").fetchone()[0],
        "near_empty": con.execute(
            "select count(*) from fulltext_md where status='ok' and chars < 200").fetchone()[0],
        "top_tags": con.execute(f"""
            select tag, count(*) c from zotero_tags group by 1 order by c desc limit {TOP_N}
        """).fetchall(),
        "top_collections": con.execute(f"""
            select collection_name, count(*) c from zotero_collections group by 1 order by c desc limit {TOP_N}
        """).fetchall(),
        "zotero_linked": con.execute("select count(*) from documents where zotero_key is not null").fetchone()[0],
    }


def render_report(stats: Dict[str, object], generated_at: datetime) -> str:
    """Markdown report text from collect_stats() output."""
    total, done, avg_chars = stats["total"], stats["done"], stats["avg_chars"]
    lines: List[str] = [
        "# Content analysis",
        "",
        f"Generated {generated_at.strftime('%Y-%m-%d %H:%M')}",
        "",
        "## Overview",
        "",
        "| | |",
        "|---|---|",
        f"| Catalogued documents | {total} |",
        # NB: the first branch has no closing pipe; kept verbatim to preserve output byte-for-byte.
        f"| Processed | {done} ({100*done/total:.1f}%)" if total else "| Processed | 0 |",
        f"| Converted to markdown | {stats['ok']} |",
        f"| Linked to a Zotero item | {stats['zotero_linked']} |",
        f"| Avg. characters per converted doc | {avg_chars:.0f} |" if avg_chars else "",
        f"| Near-empty extractions (<200 chars, likely scanned/no OCR) | {stats['near_empty']} |",
        "",
        "## By conversion status",
        "",
        "| Status | Count |",
        "|---|---|",
    ]
    lines += [f"| {s} | {c} |" for s, c in stats["by_status"]]

    lines += ["", "## By filetype", "", "| Filetype | Total | Converted OK |", "|---|---|---|"]
    lines += [f"| {ft} | {total_ft} | {ok_ft or 0} |" for ft, total_ft, ok_ft in stats["by_filetype"]]

    lines += ["", "## By category", "", "| Category | Count |", "|---|---|"]
    lines += [f"| {c} | {n} |" for c, n in stats["by_category"]]

    lines += ["", "## By source", "", "| Source | Count |", "|---|---|"]
    lines += [f"| {s} | {n} |" for s, n in stats["by_source"]]

    if stats["top_tags"]:
        lines += ["", "## Top Zotero tags", "", "| Tag | Documents |", "|---|---|"]
        lines += [f"| {t} | {c} |" for t, c in stats["top_tags"]]

    if stats["top_collections"]:
        lines += ["", "## Top Zotero collections", "", "| Collection | Documents |", "|---|---|"]
        lines += [f"| {c} | {n} |" for c, n in stats["top_collections"]]

    return "\n".join(lines)


def generate(con, out_path: Optional[Path] = None, log: Callable[[str], None] = print) -> Path:
    """Write the markdown report (default: <DATA_DIR>/ANALYSIS.md) and return its path."""
    ensure_zotero_tables(con)
    out_path = out_path or (config.DATA_DIR / "ANALYSIS.md")
    out_path.write_text(render_report(collect_stats(con), datetime.now()))
    log(f"wrote {out_path}")
    return out_path
