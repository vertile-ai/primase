"""Minimal TOML table-key edits. Used to overlay Harbor task.toml network policy and variant overrides."""

from __future__ import annotations

import json
import re
from typing import Any

_TABLE = re.compile(r"^\[([^\[\]]+)\]\s*(#.*)?$")
# [[array.of.tables]] headers end the previous table's body; they are reported as "[[name]]".
_ARRAY_TABLE = re.compile(r"^\[\[([^\[\]]+)\]\]\s*(#.*)?$")
_KEY = re.compile(r"^([A-Za-z0-9_-]+)\s*=")


def iter_tables(text: str) -> list[tuple[str, int, int]]:
    """Return (table_name, body_start, body_end) for each [table] in *text*."""
    lines = text.splitlines(keepends=True)
    spans: list[tuple[str, int, int]] = []
    current: str | None = None
    body_start = 0
    offset = 0
    for line in lines:
        m = _TABLE.match(line)
        am = None if m else _ARRAY_TABLE.match(line)
        if m or am:
            if current is not None:
                spans.append((current, body_start, offset))
            current = m.group(1).strip() if m else f"[[{am.group(1).strip()}]]"  # type: ignore[union-attr]
            body_start = offset + len(line)
        offset += len(line)
    if current is not None:
        spans.append((current, body_start, offset))
    return spans


def get_table_key(text: str, table: str, key: str) -> str | None:
    for name, start, end in iter_tables(text):
        if name != table:
            continue
        for line in text[start:end].splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            km = _KEY.match(stripped)
            if km and km.group(1) == key:
                raw = stripped.split("=", 1)[1].strip().strip("\"'")
                return raw
    return None


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return repr(value) if value != int(value) else f"{value:.1f}"
    if isinstance(value, str):
        return json.dumps(value)
    if value is None:
        return '""'
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return json.dumps(str(value))


def set_table_key(text: str, table: str, key: str, value: Any) -> str:
    """Set ``key = <value>`` inside ``[table]``, creating the table if needed.

    Nested tables such as ``[agent.env]`` are left alone. Existing comments and
    other keys in the target table are preserved. *value* may be str/int/float/bool.
    """
    rendered = f"{key} = {_format_value(value)}"
    spans = iter_tables(text)
    for name, start, end in spans:
        if name != table:
            continue
        body = text[start:end]
        lines = body.splitlines(keepends=True)
        out: list[str] = []
        replaced = False
        for line in lines:
            stripped = line.strip()
            km = _KEY.match(stripped)
            if not stripped.startswith("#") and km and km.group(1) == key:
                nl = "\n" if line.endswith("\n") else ""
                out.append(rendered + nl)
                replaced = True
            else:
                out.append(line)
        if not replaced:
            insert = rendered + "\n"
            if out and not out[-1].endswith("\n"):
                out[-1] += "\n"
            out.append(insert)
        return text[:start] + "".join(out) + text[end:]
    # table missing: append
    sep = "" if text.endswith("\n") or not text else "\n"
    return f"{text}{sep}[{table}]\n{rendered}\n"


def apply_overrides(text: str, overrides: dict[str, dict[str, Any]]) -> tuple[str, list[str]]:
    """Apply ``{table: {key: value}}`` overrides. Returns (new_text, notes)."""
    notes: list[str] = []
    out = text
    for table, keys in overrides.items():
        for key, value in keys.items():
            prev = get_table_key(out, table, key)
            out = set_table_key(out, table, key, value)
            if prev is not None:
                notes.append(f"{table}.{key} {prev!r} -> {value!r}")
            else:
                notes.append(f"{table}.{key} set to {value!r}")
    return out, notes


def append_array_tables(text: str, table: str, entries: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Append one ``[[table]]`` block per entry (scalar / list values only). Returns (new_text, notes)."""
    if not entries:
        return text, []
    out = text if (text.endswith("\n") or not text) else text + "\n"
    notes: list[str] = []
    for entry in entries:
        out += f"\n[[{table}]]\n"
        for key, value in entry.items():
            if value is None or value == {} or value == []:
                continue
            if isinstance(value, dict):
                continue  # nested tables are not rendered (keep the overlay flat)
            out += f"{key} = {_format_value(value)}\n"
        notes.append(f"[[{table}]] {entry.get('name', '?')} appended")
    return out, notes
