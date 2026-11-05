"""Money and rate formatting shared by the report, the board and the worth layer."""

from __future__ import annotations


def usd(x: float) -> str:
    return f"{'−' if round(x) < 0 else ''}${abs(x):,.0f}"


def signed(x: float) -> str:
    r = round(x)
    return "$0" if r == 0 else f"{'+' if r > 0 else '−'}${abs(x):,.0f}"


def num(x: float) -> str:
    return f"{x:,.0f}" if float(x).is_integer() else f"{x:g}"


def pct(r: float) -> str:
    return f"{r * 100:g}%"
