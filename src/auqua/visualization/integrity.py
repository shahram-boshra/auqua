# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Static HTML of a Research Integrity Report (thin; BP12 owns presentation).

Renders the report document (plain JSON values) as one self-contained page:
no scripts, no external resources, every value escaped. The sharing notice
and attributions are shown first. The module takes plain data, so it does not
depend on the analytics package (import-linter contract 7).
"""

from collections.abc import Mapping, Sequence
from html import escape
from typing import Any

_STYLE = (
    "body{font-family:system-ui,sans-serif;margin:2rem;max-width:80rem}"
    "table{border-collapse:collapse;margin:0 0 2rem}"
    "th,td{border:1px solid #999;padding:.25rem .5rem;text-align:left;vertical-align:top;"
    "font-variant-numeric:tabular-nums}"
    ".notice{border:2px solid #b00;padding:.5rem 1rem}"
    ".pass{color:#060}.warn{color:#a60}.fail{color:#b00;font-weight:bold}"
)
_FIRST = ("title", "kind", "terms")


def _text(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Mapping | list | tuple):
        return _render_value(value)
    return escape(str(value))


def _render_value(value: object) -> str:
    if isinstance(value, Mapping):
        rows = "".join(
            f"<tr><th>{escape(str(k))}</th><td>{_text(v)}</td></tr>" for k, v in value.items()
        )
        return f"<table>{rows}</table>"
    if isinstance(value, list | tuple):
        if value and all(isinstance(item, Mapping) for item in value):
            return _records(value)
        return escape(", ".join(str(item) for item in value))
    return _text(value)


def _records(items: Sequence[Mapping[str, Any]]) -> str:
    columns: list[str] = []
    for item in items:
        columns += [k for k in item if k not in columns]
    head = "".join(f"<th>{escape(c)}</th>" for c in columns)
    body = ""
    for item in items:
        cells = ""
        for c in columns:
            value = item.get(c)
            css = f' class="{escape(str(value))}"' if c == "status" else ""
            cells += f"<td{css}>{_text(value)}</td>"
        body += f"<tr>{cells}</tr>"
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render(report: Mapping[str, Any]) -> str:
    """The report as an HTML document."""
    title = escape(str(report.get("title", "Research Integrity Report")))
    terms = report.get("terms") or {}
    notice = terms.get("notice") if isinstance(terms, Mapping) else None
    parts = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        f"<title>{title}</title><style>{_STYLE}</style></head><body><h1>{title}</h1>",
    ]
    if notice:
        parts.append(f'<p class="notice">{escape(str(notice))}</p>')
    parts.append(f"<h2>terms</h2>{_render_value(terms)}")
    for key, value in report.items():
        if key in _FIRST:
            continue
        parts.append(f"<h2>{escape(key)}</h2>{_render_value(value)}")
    parts.append("</body></html>\n")
    return "".join(parts)
