"""The stores' capture files (``scripts/*-capture.js`` output in ``data/``):
the newest of each kind, each read once per change of the file.

A Walmart order-history capture runs to tens of megabytes, and the board
reloads its state whenever any input changes, so a capture is parsed once
and what is derived from it (line items, receipts, totals: small) is kept
until the file itself changes. The raw JSON is never held.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")
KEEP = 8  # derived products kept: a few kinds of capture × a few readers each

_LOCK = threading.Lock()
_CACHE: OrderedDict[tuple, object] = OrderedDict()


def newest(data: Path, pattern: str) -> Path | None:
    """The newest capture matching ``pattern`` (they are named by date)."""
    found = sorted(Path(data).glob(pattern))
    return found[-1] if found else None


def read(path: Path, derive: Callable[[list[dict]], T]) -> T:
    """``derive(the parsed capture)``, worked out once per version of the file."""
    st = Path(path).stat()
    key = (str(path), st.st_mtime_ns, st.st_size, derive.__module__, derive.__qualname__)
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]  # type: ignore[return-value]
    with open(path, encoding="utf-8") as f:
        out = derive(json.load(f))
    with _LOCK:
        _CACHE[key] = out
        while len(_CACHE) > KEEP:
            _CACHE.popitem(last=False)
    return out
