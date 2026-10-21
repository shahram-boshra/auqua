# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Static backtest report — walking skeleton (thin); owned and extended by BP12.

One self-contained HTML file built with the standard library only: no
scripts, no external resources, every value escaped. It lists the lineage of
the run, the summary, the orders with their risk decisions, and the fills.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from html import escape
from pathlib import Path


@dataclass(frozen=True)
class ReportRow:
    """One table row: cell texts in column order."""

    cells: tuple[str, ...]


@dataclass(frozen=True)
class ReportData:
    """Everything the report shows, already formatted as text."""

    title: str
    lineage: Sequence[tuple[str, str]]
    summary: Sequence[tuple[str, str]]
    order_columns: tuple[str, ...]
    orders: Sequence[ReportRow]
    fill_columns: tuple[str, ...]
    fills: Sequence[ReportRow]


_STYLE = (
    "body{font-family:system-ui,sans-serif;margin:2rem;max-width:72rem}"
    "table{border-collapse:collapse;margin:0 0 2rem}"
    "th,td{border:1px solid #999;padding:.25rem .5rem;text-align:left;"
    "font-variant-numeric:tabular-nums}"
)


def _pairs(rows: Sequence[tuple[str, str]]) -> str:
    body = "".join(f"<tr><th>{escape(k)}</th><td>{escape(v)}</td></tr>" for k, v in rows)
    return f"<table>{body}</table>"


def _table(columns: tuple[str, ...], rows: Sequence[ReportRow]) -> str:
    head = "".join(f"<th>{escape(c)}</th>" for c in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row.cells) + "</tr>" for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render(data: ReportData) -> str:
    """The report as an HTML document."""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{escape(data.title)}</title><style>{_STYLE}</style></head><body>"
        f"<h1>{escape(data.title)}</h1>"
        f"<h2>Lineage</h2>{_pairs(data.lineage)}"
        f"<h2>Summary</h2>{_pairs(data.summary)}"
        f"<h2>Orders and risk decisions</h2>{_table(data.order_columns, data.orders)}"
        f"<h2>Fills</h2>{_table(data.fill_columns, data.fills)}"
        "</body></html>\n"
    )


def write_report(path: Path, data: ReportData) -> Path:
    """Write the report to ``path`` (UTF-8) and return ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(data), encoding="utf-8")
    return path
