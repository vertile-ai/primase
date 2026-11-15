"""cursor-cli (cursor-agent) plugin: stream-json parser + included-usage budget hook."""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Any

from .base import AgentMetrics, AgentPlugin, BudgetHook

STREAM = "cursor-cli.txt"

_CLIENT_ONLY_KEYS = {
    "beforeFullFileContent",
    "afterFullFileContent",
    "relatedCursorRules",
    "relatedCursorRulePaths",
}


def tool_output(name: str, result: Any) -> tuple[int, int]:
    """(model-visible chars ≈, shell output chars) for one completed cursor tool call.

    cursor-agent's stream carries client-side extras (full before/after file contents of edits,
    rule lists), so the raw JSON overstates what the model reads. Approximation: shell →
    interleavedOutput (or stdout+stderr), read → content, edit → diffString+message, others →
    JSON of the payload minus client-only keys.
    """
    if result is None:
        return 0, 0
    if isinstance(result, str):
        return len(result), (len(result) if name == "shellToolCall" else 0)
    payload = result
    if isinstance(result, dict) and len(result) >= 1:
        inner = next(
            (result[k] for k in ("success", "failure", "error", "rejected") if isinstance(result.get(k), dict)),
            None,
        )
        payload = inner if inner is not None else result
    if not isinstance(payload, dict):
        n = len(json.dumps(payload))
        return n, 0
    if name == "shellToolCall":
        out = payload.get("interleavedOutput")
        if out is None:
            out = (payload.get("stdout") or "") + (payload.get("stderr") or "")
        return len(out), len(out)
    if name == "readToolCall" and isinstance(payload.get("content"), str):
        return len(payload["content"]), 0
    if name == "editToolCall":
        return len(payload.get("diffString") or "") + len(payload.get("message") or ""), 0
    return len(json.dumps({k: v for k, v in payload.items() if k not in _CLIENT_ONLY_KEYS})), 0


def parse_cursor_stream(path: Path) -> AgentMetrics:
    """Aggregate a cursor-agent stream-json log. Unknown/invalid lines are ignored."""
    out = AgentMetrics()
    if not path.exists():
        return out
    model_calls: set[str] = set()
    tool_calls = 0
    shell_calls = 0
    tool_chars = 0
    shell_chars = 0
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        t = ev.get("type")
        if t == "system":
            out.model = ev.get("model") or out.model
        elif t == "assistant" and ev.get("model_call_id"):
            model_calls.add(ev["model_call_id"])
        elif t == "tool_call":
            if ev.get("model_call_id"):
                model_calls.add(ev["model_call_id"])
            if ev.get("subtype") == "completed":
                for name, call in (ev.get("tool_call") or {}).items():
                    if not isinstance(call, dict):
                        continue
                    tool_calls += 1
                    shell_calls += int(name == "shellToolCall")
                    vis, sh = tool_output(name, call.get("result"))
                    tool_chars += vis
                    shell_chars += sh
        elif t == "result":
            out.result_events += 1
            out.is_error = bool(ev.get("is_error"))
            u = ev.get("usage")
            if isinstance(u, dict):
                out.has_usage = True
                out.input_tokens += int(u.get("inputTokens") or 0)
                out.output_tokens += int(u.get("outputTokens") or 0)
                out.cache_read_tokens += int(u.get("cacheReadTokens") or 0)
                out.cache_write_tokens += int(u.get("cacheWriteTokens") or 0)
                c = u.get("totalCost", u.get("cost"))
                if c is not None:
                    out.reported_cost_usd = (out.reported_cost_usd or 0.0) + float(c)
                    out.cost_source = "cursor_cli"
    out.n_turns = len(model_calls)
    out.n_tool_calls = tool_calls
    out.n_shell_calls = shell_calls
    out.tool_output_chars = tool_chars
    out.shell_output_chars = shell_chars
    return out


class IncludedModelsHook(BudgetHook):
    """Reject models that do not match Cursor included-usage allow-list patterns."""

    def __init__(self, patterns: list[str], allow_non_included: bool = False) -> None:
        self.patterns = list(patterns)
        self.allow_non_included = allow_non_included

    def check_model(self, model_id: str) -> None:
        if self.allow_non_included or not self.patterns:
            return
        if not any(fnmatch.fnmatchcase(model_id, p) for p in self.patterns):
            from ..budget import BudgetExceeded

            raise BudgetExceeded(
                f"model {model_id!r} is not in cursor-cli included_models {self.patterns}; "
                "set agent.plugin.allow_non_included_models: true to bill credits"
            )


class CursorCliPlugin(AgentPlugin):
    name = "cursor-cli"
    billed_cost_sources = frozenset({"cursor_cli"})

    def collect(self, agent_dir: Path) -> AgentMetrics:
        return parse_cursor_stream(Path(agent_dir) / STREAM)

    def budget_hooks(self, plugin_config: dict[str, Any] | None = None) -> list[BudgetHook]:
        cfg = plugin_config or {}
        patterns = list(cfg.get("included_models") or [])
        allow = bool(cfg.get("allow_non_included_models", False))
        if not patterns and not allow:
            return []
        return [IncludedModelsHook(patterns, allow_non_included=allow)]
