"""To-do rows (``penny board --todo-file``): any Markdown table whose rows
carry ``<!-- id:penny-... -->``. Called "wiki" in keys and groups, after the
first such file."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

ISO_DATE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
WIKI_ID = re.compile(r"<!--\s*id:(penny-[\w-]+)\s*-->")
DOC_REF = re.compile(r"docs/[\w-]+(?:/[\w-]+)*\.md")

@dataclass
class WikiRow:
    slug: str
    item: str
    detail: str
    heading: str = ""


def parse_wiki(text: str) -> list[WikiRow]:
    """Open items rows whose ``<!-- id:slug -->`` starts with ``penny-``."""
    out: list[WikiRow] = []
    heading = ""
    for line in text.splitlines():
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            continue
        m = WIKI_ID.search(line)
        if not m or not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        if len(cells) < 2:
            continue
        item = WIKI_ID.sub("", cells[0]).replace("**", "").strip()
        out.append(WikiRow(m.group(1), item, cells[1], heading))
    return out


def future_dates(text: str, today: date) -> list[date]:
    out = []
    for s in ISO_DATE.findall(text):
        try:
            d = date.fromisoformat(s)
        except ValueError:
            continue
        if d > today:
            out.append(d)
    return sorted(out)
