"""The one way penny replaces a file: a temp file beside it, fsynced, then
``os.replace``, then the directory fsynced, so a crash or power cut leaves
the old file or the new one, never a torn or empty one."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_atomic(path: Path, data: str | bytes, *, mode: int | None = None) -> None:
    """Replace ``path`` with ``data``. The new file keeps the old one's
    permissions; a file that didn't exist gets ``mode``, 0600 by default,
    since nearly everything penny writes is personal."""
    path = Path(path)
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


def fsync_dir(d: Path) -> None:
    """Make a rename in ``d`` durable: without it a crash can leave the old
    name pointing at the old file, or at nothing."""
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
