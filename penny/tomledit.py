"""Line-based, comment-preserving edits to an existing TOML file.

``tomllib`` reads TOML and nothing in the stdlib writes it. The comments in
rules.toml and assumptions.toml are the documentation, so this never
re-serialises: it replaces one value on one line and leaves every other byte
alone. ``set_key`` and ``set_inline`` never append: a missing section or key
raises ``KeyError``. ``override`` is the one exception, for an instance
``rules.toml``, whose keys override penny's defaults: a key the defaults have
and the instance doesn't yet is added to the instance.

Every edit is parsed back with ``tomllib`` before it is returned, and
``write_edits`` replaces the file atomically under a process-wide lock.
"""

from __future__ import annotations

import math
import re
import threading
import tomllib
from collections.abc import Callable
from pathlib import Path

from . import fsio

LOCK = threading.RLock()  # re-entrant: a batch holds it across several write_edits

_HEADER = re.compile(r"^\s*\[")
_PAIR = re.compile(r"[\s,]*([A-Za-z0-9_-]+)\s*=\s*")
_LINE = re.compile(r"[^\n]*\n|[^\n]+")


def fmt(value) -> str:
    """A Python value as a TOML literal: bool, int, float or basic string."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"not a finite number: {value!r}")
        s = repr(value)
        if "." in s and "e" not in s:
            s = s.rstrip("0").rstrip(".")
        return s
    if isinstance(value, str):
        out = []
        for ch in value:
            if ch in ('"', "\\"):
                out.append("\\" + ch)
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\t":
                out.append("\\t")
            elif ord(ch) < 0x20 or ord(ch) == 0x7F:
                out.append(f"\\u{ord(ch):04X}")
            else:
                out.append(ch)
        return '"' + "".join(out) + '"'
    raise TypeError(f"cannot write {type(value).__name__} to TOML")


def _header_name(line: str) -> str | None:
    """``[a.b]`` → ``a.b``; None for anything that isn't a table header."""
    s = line.strip()
    if not s.startswith("[") or s.startswith("[["):
        return None
    end = s.find("]")
    if end < 0:
        return None
    rest = s[end + 1 :].strip()
    if rest and not rest.startswith("#"):
        return None
    return ".".join(p.strip() for p in s[1:end].split("."))


def _scan_value(s: str, i: int, stops: str = "#") -> int:
    """Index just past the value starting at ``s[i]``.

    Strings and brackets are skipped whole, so a ``#`` or ``,`` inside them is
    not mistaken for the end. A bare value ends at the first stop character.
    """
    if s.startswith(('"""', "'''"), i):
        raise ValueError("multi-line strings are not edited here")
    c = s[i] if i < len(s) else ""
    if c == '"':
        j = i + 1
        while j < len(s):
            if s[j] == "\\":
                j += 2
                continue
            if s[j] == '"':
                return j + 1
            j += 1
        raise ValueError("unterminated string")
    if c == "'":
        j = s.find("'", i + 1)
        if j < 0:
            raise ValueError("unterminated string")
        return j + 1
    if c in "[{":
        depth, j = 0, i
        while j < len(s):
            ch = s[j]
            if ch in "\"'":
                j = _scan_value(s, j)
                continue
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        raise ValueError("array or inline table spans lines; not edited here")
    j = i
    while j < len(s) and s[j] not in stops and s[j] not in "\r\n":
        j += 1
    while j > i and s[j - 1] in " \t":
        j -= 1
    return j


def _find_line(lines: list[str], section: str, key: str) -> tuple[int, int, int]:
    """(line index, value start, value end) of ``key`` in ``[section]``."""
    want = ".".join(p.strip() for p in section.split("."))
    start = next((n for n, ln in enumerate(lines) if _header_name(ln) == want), None)
    if start is None:
        raise KeyError(f"no [{section}] section")
    pat = re.compile(r"^\s*" + re.escape(key) + r"\s*=\s*")
    for n in range(start + 1, len(lines)):
        if _HEADER.match(lines[n]):
            break
        m = pat.match(lines[n])
        if m:
            return n, m.end(), _scan_value(lines[n], m.end())
    raise KeyError(f"no {key} in [{section}]")


def _read_back(text: str, section: str, key: str, subkey: str | None):
    node = tomllib.loads(text)
    for part in [p.strip() for p in section.split(".")] + [key] + ([subkey] if subkey else []):
        node = node[part]
    return node


def _check(text: str, section: str, key: str, subkey: str | None, value) -> str:
    got = _read_back(text, section, key, subkey)
    if got != value or isinstance(got, bool) != isinstance(value, bool):
        raise ValueError(f"[{section}] {key} read back as {got!r}, expected {value!r}")
    return text


def set_key(text: str, section: str, key: str, value) -> str:
    """Replace the value of ``key`` in ``[section]``, keeping indentation and
    any trailing comment."""
    lines = _LINE.findall(text)  # on \n only; str.splitlines also splits on U+2028 and friends
    n, a, b = _find_line(lines, section, key)
    line = lines[n]
    lines[n] = line[:a] + fmt(value) + line[b:]
    return _check("".join(lines), section, key, None, value)


def set_inline(text: str, section: str, key: str, subkey: str, value) -> str:
    """Replace ``subkey`` inside a single-line inline table, such as
    ``rates = { restaurants = 0.045 }``."""
    lines = _LINE.findall(text)  # on \n only; str.splitlines also splits on U+2028 and friends
    n, a, b = _find_line(lines, section, key)
    line = lines[n]
    if not line.startswith("{", a):
        raise KeyError(f"[{section}] {key} is not an inline table")
    i = a + 1
    while i < b - 1:
        m = _PAIR.match(line, i)
        if not m:
            break
        end = _scan_value(line, m.end(), stops=",}")
        if m.group(1) == subkey:
            lines[n] = line[: m.end()] + fmt(value) + line[end:]
            return _check("".join(lines), section, key, subkey, value)
        i = end
    raise KeyError(f"no {subkey} in [{section}] {key}")


def _section_end(lines: list[str], section: str) -> int | None:
    """Index just past the last non-blank line of ``[section]``, or None."""
    want = ".".join(p.strip() for p in section.split("."))
    start = next((n for n, ln in enumerate(lines) if _header_name(ln) == want), None)
    if start is None:
        return None
    end = start + 1
    for n in range(start + 1, len(lines)):
        if _HEADER.match(lines[n]):
            break
        if lines[n].strip():
            end = n + 1
    return end


def override(text: str, section: str, key: str, subkey: str | None, value) -> str:
    """Set ``[section] key`` (or ``key.subkey`` in an inline table), adding it
    when the file doesn't have it yet: the key goes at the end of its section,
    a subkey into the key's inline table, and a missing section is appended
    at the end of the file. Existing lines keep every byte but the value."""
    try:
        return set_inline(text, section, key, subkey, value) if subkey else set_key(text, section, key, value)
    except KeyError:
        pass
    lines = _LINE.findall(text)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    entry = f"{key} = {{ {subkey} = {fmt(value)} }}\n" if subkey else f"{key} = {fmt(value)}\n"
    end = _section_end(lines, section)
    if end is None:
        lines += (["\n"] if lines and lines[-1].strip() else []) + [f"[{section}]\n", entry]
    else:
        try:
            n, a, b = _find_line(lines, section, key)
        except KeyError:
            lines.insert(end, entry)
        else:
            line = lines[n]
            if not (subkey and line.startswith("{", a)):
                raise KeyError(f"[{section}] {key} is not an inline table")
            inner = line[a + 1 : b - 1].strip()
            lines[n] = line[:a] + "{ " + (inner + ", " if inner else "") + f"{subkey} = {fmt(value)} }}" + line[b:]
    return _check("".join(lines), section, key, subkey, value)


def write_edits(path: str | Path, edit: Callable[[str], str]) -> tuple[str, str]:
    """Apply ``edit`` to the file's text and replace the file atomically.

    Returns (before, after). A caller that changes several files together
    holds ``LOCK`` around all of them.
    """
    path = Path(path)
    with LOCK:
        before = path.read_text()
        after = edit(before)
        fsio.write_atomic(path, after)
        return before, after
