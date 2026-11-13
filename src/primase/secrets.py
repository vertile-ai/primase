"""Secrets: loaded only from the process environment or an env-file, never from config/CLI args.

Every value we load is registered with a :class:`Redactor`; all harness logs go through it and every
job directory is scrubbed after a trial (backends write raw agent stdout, and an agent can always
run ``env``).
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

REDACTED = "***REDACTED***"
_MIN_SECRET_LEN = 8
_TEXT_SUFFIXES = {
    "",
    ".txt",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".yaml",
    ".yml",
    ".toml",
    ".csv",
    ".patch",
    ".out",
    ".err",
    ".html",
}
_MAX_SCRUB_BYTES = 200 * 1024 * 1024


def parse_env_file(path: str | Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` / ``export KEY=VALUE`` lines (shell-style quoting, ``#`` comments)."""
    out: dict[str, str] = {}
    for raw in Path(path).expanduser().read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        try:
            parts = shlex.split(value, comments=True, posix=True)
        except ValueError:
            parts = [value.strip()]
        out[key] = parts[0] if parts else ""
    return out


@dataclass
class Redactor:
    values: set[str] = field(default_factory=set)

    def add(self, value: str | None) -> None:
        if value and len(value) >= _MIN_SECRET_LEN:
            self.values.add(value)

    def __call__(self, text: str) -> str:
        for v in sorted(self.values, key=len, reverse=True):
            if v in text:
                text = text.replace(v, REDACTED)
        return text

    def contains_secret(self, text: str) -> bool:
        return any(v in text for v in self.values)

    def scrub_tree(self, root: str | Path) -> list[Path]:
        """Replace secret values in every text-ish file under ``root``. Returns files that were rewritten."""
        hits: list[Path] = []
        if not self.values:
            return hits
        needles = [v.encode() for v in self.values]
        for p in Path(root).rglob("*"):
            if not p.is_file() or p.is_symlink() or p.suffix.lower() not in _TEXT_SUFFIXES:
                continue
            try:
                if p.stat().st_size > _MAX_SCRUB_BYTES:
                    continue
                data = p.read_bytes()
            except OSError:
                continue
            if not any(n in data for n in needles):
                continue
            for n in sorted(needles, key=len, reverse=True):
                data = data.replace(n, REDACTED.encode())
            p.write_bytes(data)
            hits.append(p)
        return hits


@dataclass
class SecretStore:
    """Resolved secrets for one run. ``env()`` is what backends merge into the subprocess env."""

    names: list[str]
    _values: dict[str, str] = field(default_factory=dict, repr=False)
    redactor: Redactor = field(default_factory=Redactor, repr=False)

    @classmethod
    def load(
        cls,
        names: list[str],
        env_file: str | Path | None = None,
        environ: dict[str, str] | None = None,
    ) -> SecretStore:
        environ = dict(os.environ if environ is None else environ)
        file_vals: dict[str, str] = {}
        if env_file:
            p = Path(env_file).expanduser()
            if p.exists():
                mode = p.stat().st_mode & 0o077
                if mode:
                    raise PermissionError(f"{p} is readable by group/others (chmod 600 it)")
                file_vals = parse_env_file(p)
        store = cls(names=list(names))
        for n in names:
            v = environ.get(n) or file_vals.get(n)
            if v:
                store._values[n] = v
                store.redactor.add(v)
        return store

    def missing(self) -> list[str]:
        return [n for n in self.names if n not in self._values]

    def env(self) -> dict[str, str]:
        return dict(self._values)

    def describe(self) -> dict[str, str]:
        return {n: ("set" if n in self._values else "MISSING") for n in self.names}

    def __repr__(self) -> str:  # never leak values through repr/logging
        return f"SecretStore({self.describe()})"
