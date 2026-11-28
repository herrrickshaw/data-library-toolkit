"""Read one file out of a zip or tar without unpacking the rest.

Catalogue paths look like "folder/a.zip!inner/report.pdf" (one "!" per level of
nesting). Member names come from untrusted archives, so nothing is ever written
to a path they choose: the caller supplies the destination.
"""
from __future__ import annotations

import re
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import List, Optional

_SPLIT = re.compile(r"(?<=\.zip)!|(?<=\.tar)!|(?<=\.tar\.gz)!|(?<=\.tgz)!", re.I)


def split_member_path(path: str) -> Optional[List[str]]:
    """[outer archive, member, ...] or None if `path` is not inside a supported archive.

    A "!" that does not follow an archive extension is part of a file name. Paths
    with ".." or an absolute member are refused.
    """
    parts = _SPLIT.split(path)
    if len(parts) < 2:
        return None
    for member in parts[1:]:
        pure = PurePosixPath(member)
        if not member or pure.is_absolute() or ".." in pure.parts:
            return None
    return parts


def _copy_member(archive: Path, member: str, dest: Path, max_bytes: int) -> Optional[str]:
    """Copy one regular file out of `archive` to `dest`; returns an error message or None."""
    try:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as zf:
                info = zf.getinfo(member)
                if info.is_dir() or info.file_size > max_bytes:
                    return "member is a folder or exceeds the size limit"
                with zf.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
        else:
            with tarfile.open(archive) as tf:
                info = tf.getmember(member)
                if not info.isfile() or info.size > max_bytes:
                    return "member is not a regular file or exceeds the size limit"
                with tf.extractfile(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
    except KeyError:
        return f"{member!r} not found in archive"
    except Exception as e:  # noqa: BLE001 — corrupt or encrypted archives
        return f"could not read archive: {str(e)[-300:]}"
    return None


def materialize(archive: Path, members: List[str], dest: Path, max_bytes: int) -> Optional[str]:
    """Write the file at members[0]!members[1]!... inside `archive` to `dest`."""
    if not archive.exists():
        return "archive could not be fetched"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        current = archive
        for level, member in enumerate(members[:-1]):
            inner = Path(tmp) / f"level{level}"
            err = _copy_member(current, member, inner, max_bytes)
            if err:
                return err
            current = inner
        err = _copy_member(current, members[-1], dest, max_bytes)
    if err:
        dest.unlink(missing_ok=True)
    return err
