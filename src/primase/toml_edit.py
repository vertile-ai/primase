"""Minimal TOML table-key edits. Used to overlay Harbor task.toml network policy."""

from __future__ import annotations

import re

_TABLE = re.compile(r"^\[([^\]]+)\]\s*$")
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
        if m:
            if current is not None:
                spans.append((current, body_start, offset))
            current = m.group(1)
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


def set_table_key(text: str, table: str, key: str, value: str) -> str:
    """Set ``key = "value"`` inside ``[table]``, creating the table if needed.

    Nested tables such as ``[agent.env]`` are left alone. Existing comments and
    other keys in the target table are preserved.
    """
    quoted = f'{key} = "{value}"'
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
                out.append(quoted + nl)
                replaced = True
            else:
                out.append(line)
        if not replaced:
            # insert after any leading blank lines
            insert = quoted + "\n"
            if out and not out[-1].endswith("\n"):
                out[-1] += "\n"
            out.append(insert)
        return text[:start] + "".join(out) + text[end:]
    # table missing: append
    sep = "" if text.endswith("\n") or not text else "\n"
    return f"{text}{sep}[{table}]\n{quoted}\n"
