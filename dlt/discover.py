"""
Discover document files across one or more rclone remotes and add them to
the `documents` table.

Why not one flat `rclone lsf -R` per remote: on any sufficiently large
remote (a personal cloud account with years of accumulated folders, a
shared drive, a machine-backup mirror) a single recursive listing call can
itself time out before producing any output at all. The fix — used
throughout this module — is recursive subdivision: try a bounded, timed
listing of a folder; if it times out, list just that folder's direct
subfolders (fast, non-recursive) and recurse into each independently, going
deeper only where needed. A branch that still can't be listed at
config.DISCOVERY_MAX_DEPTH is logged and skipped rather than blocking the
whole run.

Checkpointed (one line per top-level unit swept, per remote) so an
interrupted run resumes instead of re-scanning everything.
"""
import hashlib
import json
import subprocess
from pathlib import Path

from . import config

CHECKPOINT_FILE = config.DATA_DIR / "discover_checkpoint.jsonl"


def _run(cmd, timeout):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def rclone_lsd(remote, timeout=60):
    """Direct subfolders only (fast, non-recursive). None on failure/timeout
    — a folder whose *subfolder listing* itself times out is unlistable,
    same as a failed recursive scan, not a crash."""
    result = _run([config.RCLONE_BIN, "lsf", remote, "--dirs-only"], timeout)
    if result is None or result.returncode != 0:
        return None
    return [d.rstrip("/") for d in result.stdout.splitlines() if d.strip()]


def rclone_lsf_recursive(remote, timeout):
    """Full recursive file listing of one folder. None on timeout (caller
    subdivides)."""
    result = _run(
        [config.RCLONE_BIN, "lsf", remote, "-R", "--files-only",
         "--format", "pst", "--separator", "\t"],
        timeout,
    )
    if result is None or result.returncode != 0:
        return None
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            rows.append(tuple(parts))
    return rows


def _extract(rows, folder_path, found, archives, source=None):
    for rel_path, size_str, mtime_str in rows:
        full_path = f"{folder_path}/{rel_path}" if folder_path else rel_path
        ext = Path(full_path).suffix.lstrip(".").lower()
        if ext in config.DOCUMENT_EXTENSIONS:
            found.append((full_path, size_str, mtime_str))
        elif ext in {"zip", "tar", "gz", "tgz"}:
            archives.append({"path": full_path, "source": source})


def walk(remote_root, folder_path, depth, found, archives, skipped, source=None):
    source = source or remote_root
    target = f"{remote_root}{folder_path}" if folder_path else remote_root
    rows = rclone_lsf_recursive(target, config.DISCOVERY_PER_CALL_TIMEOUT_S)
    if rows is not None:
        _extract(rows, folder_path, found, archives, source)
        return

    if depth >= config.DISCOVERY_MAX_DEPTH:
        skipped.append({"path": target, "reason": f"timed out at max depth {config.DISCOVERY_MAX_DEPTH}"})
        return

    subfolders = rclone_lsd(target, timeout=60)
    if not subfolders:
        # No subfolders (or lsd itself failed) but -R still timed out: retry
        # once with 3x the budget rather than recursing into nothing.
        rows = rclone_lsf_recursive(target, config.DISCOVERY_PER_CALL_TIMEOUT_S * 3)
        if rows is not None:
            _extract(rows, folder_path, found, archives, source)
        else:
            skipped.append({"path": target, "reason": "no subfolders but still times out"})
        return

    for sub in subfolders:
        sub_path = f"{folder_path}/{sub}" if folder_path else sub
        walk(remote_root, sub_path, depth + 1, found, archives, skipped, source)


def item_key_for(source, path):
    return "AUTO_" + hashlib.md5(f"{source}:{path}".encode()).hexdigest()[:12]


def _mtime_ms(mtime_str):
    import datetime
    try:
        dt = datetime.datetime.strptime(mtime_str[:19], "%Y-%m-%d %H:%M:%S")
        return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
    except ValueError:
        return None


def _load_checkpoint():
    done, all_found, all_archives, all_skipped = set(), [], [], []
    if not CHECKPOINT_FILE.exists():
        return done, all_found, all_archives, all_skipped
    with open(CHECKPOINT_FILE) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            done.add((rec["remote"], rec["folder"]))
            for path, sz, mt in rec.get("found", []):
                all_found.append((rec["source"], path, sz, mt))
            all_archives.extend(rec.get("archives", []))
            all_skipped.extend(rec.get("skipped", []))
    return done, all_found, all_archives, all_skipped


def _checkpoint(remote, folder, source, found, archives, skipped):
    with open(CHECKPOINT_FILE, "a") as f:
        f.write(json.dumps({"remote": remote, "folder": folder, "source": source,
                             "found": found, "archives": archives, "skipped": skipped}) + "\n")


def scan(remotes=None, fresh=False, log=print):
    """Walk every configured remote, returns (found, archives, skipped).
    found/archives/skipped accumulate via a checkpoint file, so re-running
    after an interruption picks up where it left off."""
    remotes = remotes or config.RCLONE_REMOTES
    if fresh:
        CHECKPOINT_FILE.unlink(missing_ok=True)
    done_units, all_found, all_archives, all_skipped = _load_checkpoint()
    if done_units:
        log(f"resuming: {len(done_units)} unit(s) already swept, {len(all_found)} document(s) carried forward")

    for remote in remotes:
        source = remote
        log(f"=== sweeping {remote} ===")
        top_folders = rclone_lsd(remote, timeout=60) or []
        log(f"{len(top_folders)} top-level folder(s)")

        if (remote, "") not in done_units:
            found, archives = [], []
            result = _run(
                [config.RCLONE_BIN, "lsf", remote, "--files-only", "--format", "pst", "--separator", "\t"],
                60,
            )
            if result is not None and result.returncode == 0:
                rows = [tuple(l.split("\t")) for l in result.stdout.splitlines() if len(l.split("\t")) == 3]
                _extract(rows, "", found, archives, source)
            all_found.extend((source, p, sz, mt) for p, sz, mt in found)
            all_archives.extend(archives)
            _checkpoint(remote, "", source, found, archives, [])

        for folder in top_folders:
            if folder.lower() in config.SKIP_FOLDERS:
                log(f"skip {remote}{folder} (configured skip list)")
                _checkpoint(remote, folder, source, [], [], [{"path": f"{remote}{folder}", "reason": "configured_skip"}])
                continue
            if (remote, folder) in done_units:
                continue
            found, archives, skipped = [], [], []
            walk(remote, folder, 0, found, archives, skipped, source)
            all_found.extend((source, p, sz, mt) for p, sz, mt in found)
            all_archives.extend(archives)
            all_skipped.extend(skipped)
            _checkpoint(remote, folder, source, found, archives, skipped)

    log(f"\nscan complete: {len(all_found)} document file(s), {len(all_archives)} archive(s), "
        f"{len(all_skipped)} folder(s) skipped.")
    return all_found, all_archives, all_skipped


def insert_new(con, found, categorize_fn=None, dry_run=False, log=print):
    """categorize_fn(path) -> (category, topic); defaults to ('uncategorized', top-level folder)."""
    existing_paths = {r[0] for r in con.execute("select path from documents").fetchall()}
    new_rows = [(s, p, sz, mt) for s, p, sz, mt in found if p not in existing_paths]
    log(f"catalog has {len(existing_paths)} path(s); {len(new_rows)} new of {len(found)} found")
    if not new_rows:
        return 0

    def default_categorize(path):
        top = path.split("/")[0] if "/" in path else "(root)"
        return "uncategorized", top

    categorize_fn = categorize_fn or default_categorize

    prepared = []
    for source, path, size_str, mtime_str in new_rows:
        item_key = item_key_for(source, path)
        title = Path(path).name
        filetype = Path(path).suffix.lstrip(".").lower() or "unknown"
        category, topic = categorize_fn(path)
        mtime_ms = _mtime_ms(mtime_str)
        prepared.append((item_key, title, path, source, category, topic, filetype,
                          None, None, False, None, mtime_ms))

    if dry_run:
        log("--dry-run: not writing to the database.")
        return len(prepared)

    before = con.execute("select count(*) from documents").fetchone()[0]
    con.executemany("""
        insert into documents (item_key, title, path, source, category, topic,
                                filetype, year, md5, is_duplicate, duplicate_of, mtime)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict (item_key) do nothing
    """, prepared)
    after = con.execute("select count(*) from documents").fetchone()[0]
    log(f"inserted {after - before} new row(s) ({before} -> {after})")
    return after - before
