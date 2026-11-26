"""OpenAI Codex CLI plugin: rollout-*.jsonl collectors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import AgentMetrics, AgentPlugin


def _codex_token_payload(ev: dict[str, Any]) -> dict[str, Any] | None:
    payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else ev
    if not isinstance(payload, dict):
        return None
    inner = payload.get("info") if isinstance(payload.get("info"), dict) else payload
    usage = None
    if isinstance(inner, dict):
        usage = inner.get("total_token_usage") or inner.get("last_token_usage") or inner.get("token_usage")
        if usage is None and payload.get("type") in {"token_count", "event_msg"}:
            usage = inner if "input_tokens" in inner or "output_tokens" in inner else None
    if ev.get("type") == "token_count" and isinstance(ev.get("last_token_usage"), dict):
        usage = ev["last_token_usage"]
    return usage if isinstance(usage, dict) else None


def parse_codex_rollouts(agent_dir: Path) -> AgentMetrics:
    """Parse ``agent/sessions/**/rollout-*.jsonl``.

    Uses the last ``token_count`` event's ``info.total_token_usage`` (cumulative over the session;
    ``last_token_usage`` is only the latest turn and is a fallback). Codex's ``input_tokens`` INCLUDES
    ``cached_input_tokens``, so non-cached input = input_tokens - cached_input_tokens.
    """
    out = AgentMetrics()
    sessions = agent_dir / "sessions"
    if not sessions.exists():
        return out
    files = sorted(p for p in sessions.rglob("rollout-*.jsonl") if p.is_file())
    if not files:
        return out
    last: dict[str, Any] | None = None
    n_items = 0
    n_tools = 0
    tool_chars = 0
    for path in files:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            typ = ev.get("type")
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            ptype = payload.get("type") if isinstance(payload, dict) else None
            usage = _codex_token_payload(ev)
            if usage:
                last = usage
            if typ == "response_item" or ptype == "response_item":
                n_items += 1
            item = payload.get("item") if isinstance(payload, dict) else None
            if isinstance(item, dict) and item.get("type") == "function_call":
                n_tools += 1
            if isinstance(item, dict) and item.get("type") == "function_call_output":
                c = item.get("output")
                if isinstance(c, str):
                    tool_chars += len(c)
    if last:
        out.has_usage = True
        out.cache_read_tokens = int(last.get("cached_input_tokens") or 0)
        out.input_tokens = int(last.get("input_tokens") or 0) - out.cache_read_tokens
        out.output_tokens = int(last.get("output_tokens") or 0)
        out.cache_write_tokens = int(last.get("cache_write_input_tokens") or 0)
        if last.get("cost") is not None:
            out.cost_source = "codex"
            out.reported_cost_usd = float(last["cost"])
    out.n_turns = n_items or out.n_turns
    out.n_tool_calls = n_tools
    out.tool_output_chars = tool_chars
    return out


class CodexPlugin(AgentPlugin):
    name = "codex"
    billed_cost_sources = frozenset({"codex"})

    def collect(self, agent_dir: Path) -> AgentMetrics:
        return parse_codex_rollouts(Path(agent_dir))
