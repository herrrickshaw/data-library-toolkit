"""
Generate a markdown report summarizing the catalog + conversion results +
(if linked) Zotero tags/collections — the "insights" layer over the raw
tables, meant to be read by a person, not queried.
"""
from datetime import datetime

from . import config
from .zotero_index import ensure_zotero_tables


def generate(con, out_path=None, log=print):
    ensure_zotero_tables(con)
    out_path = out_path or (config.DATA_DIR / "ANALYSIS.md")

    total = con.execute("select count(*) from documents where is_duplicate=false").fetchone()[0]
    done = con.execute("select count(*) from fulltext_md").fetchone()[0]
    ok = con.execute("select count(*) from fulltext_md where status='ok'").fetchone()[0]

    by_status = con.execute("select status, count(*) from fulltext_md group by 1 order by 2 desc").fetchall()
    by_filetype = con.execute("""
        select d.filetype, count(*), sum(case when f.status='ok' then 1 else 0 end)
        from documents d left join fulltext_md f on f.item_key=d.item_key
        where d.is_duplicate=false group by 1 order by 2 desc limit 20
    """).fetchall()
    by_category = con.execute("""
        select category, count(*) from documents where is_duplicate=false group by 1 order by 2 desc
    """).fetchall()
    by_source = con.execute("""
        select source, count(*) from documents where is_duplicate=false group by 1 order by 2 desc
    """).fetchall()
    avg_chars = con.execute("select avg(chars) from fulltext_md where status='ok'").fetchone()[0]
    near_empty = con.execute("select count(*) from fulltext_md where status='ok' and chars < 200").fetchone()[0]

    top_tags = con.execute("""
        select tag, count(*) c from zotero_tags group by 1 order by c desc limit 20
    """).fetchall()
    top_collections = con.execute("""
        select collection_name, count(*) c from zotero_collections group by 1 order by c desc limit 20
    """).fetchall()
    zotero_linked = con.execute("select count(*) from documents where zotero_key is not null").fetchone()[0]

    lines = [
        f"# Content analysis",
        f"",
        f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"",
        f"## Overview",
        f"",
        f"| | |",
        f"|---|---|",
        f"| Catalogued documents | {total} |",
        f"| Processed | {done} ({100*done/total:.1f}%)" if total else "| Processed | 0 |",
        f"| Converted to markdown | {ok} |",
        f"| Linked to a Zotero item | {zotero_linked} |",
        f"| Avg. characters per converted doc | {avg_chars:.0f} |" if avg_chars else "",
        f"| Near-empty extractions (<200 chars, likely scanned/no OCR) | {near_empty} |",
        f"",
        f"## By conversion status",
        f"",
        f"| Status | Count |",
        f"|---|---|",
    ]
    lines += [f"| {s} | {c} |" for s, c in by_status]

    lines += ["", "## By filetype", "", "| Filetype | Total | Converted OK |", "|---|---|---|"]
    lines += [f"| {ft} | {total_ft} | {ok_ft or 0} |" for ft, total_ft, ok_ft in by_filetype]

    lines += ["", "## By category", "", "| Category | Count |", "|---|---|"]
    lines += [f"| {c} | {n} |" for c, n in by_category]

    lines += ["", "## By source", "", "| Source | Count |", "|---|---|"]
    lines += [f"| {s} | {n} |" for s, n in by_source]

    if top_tags:
        lines += ["", "## Top Zotero tags", "", "| Tag | Documents |", "|---|---|"]
        lines += [f"| {t} | {c} |" for t, c in top_tags]

    if top_collections:
        lines += ["", "## Top Zotero collections", "", "| Collection | Documents |", "|---|---|"]
        lines += [f"| {c} | {n} |" for c, n in top_collections]

    out_path.write_text("\n".join(l for l in lines if l is not None))
    log(f"wrote {out_path}")
    return out_path
