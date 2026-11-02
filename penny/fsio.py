"""The one way penny replaces a file: a temp file beside it, fsynced, then
``os.replace``, then the directory fsynced, so a crash or power cut leaves
the old file or the new one, never a torn or empty one."""

from __future__ import annotations

import fcntl
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


def write_atomic(path: Path, data: str | bytes, *, mode: int | None = None) -> None:
    """Replace ``path`` with ``data``. The new file keeps the old one's
    permissions; a file that didn't exist gets ``mode``, 0600 by default,
    since nearly everything penny writes is personal."""
    # Through a symlink to the file it names: replacing the link itself would
    # leave the target stale and the edit in a new file beside it.
    path = Path(os.path.realpath(path))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if mode is None:
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    raw = data.encode() if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    fsync_dir(path.parent)


@contextmanager
def locked(path: Path):
    """An exclusive flock across processes for a read-modify-write of
    ``path``, on a ``.<name>.lock`` beside it: the CLI and the board saving one
    file at once each keep the other's change."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path.parent / f".{path.name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def append_lines(path: Path, lines: list[str]) -> None:
    """Append whole lines to a log (JSON Lines), fsynced; a new file is 0600.
    A crash mid-append can leave a torn last line: the next append starts on a
    fresh line, so the tear stays one bad line a reader can skip, and never
    swallows the record after it."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "r+b") as f:
        size = f.seek(0, os.SEEK_END)
        lead = b""
        if size:
            f.seek(size - 1)
            if f.read(1) != b"\n":
                lead = b"\n"
        f.write(lead + b"".join(ln.encode() + b"\n" for ln in lines))
        f.flush()
        os.fsync(f.fileno())


def read_lines(path: Path, loads) -> tuple[list, int]:
    """Every line of a JSON Lines log through ``loads``, and how many were
    torn and skipped."""
    path = Path(path)
    if not path.exists():
        return [], 0
    out, bad = [], 0
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            out.append(loads(line))
        except ValueError:
            bad += 1
    return out, bad


def fsync_dir(d: Path) -> None:
    """Make a rename in ``d`` durable: without it a crash can leave the old
    name pointing at the old file, or at nothing."""
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
