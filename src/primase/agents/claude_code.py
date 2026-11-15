"""Claude Code plugin: stdout stream-json + session jsonl collectors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import AgentMetrics, AgentPlugin

STREAM = "claude-code.txt"


def _claude_usage_fields(u: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        int(u.get("input_tokens") or 0),
        int(u.get("output_tokens") or 0),
        int(u.get("cache_read_input_tokens") or 0),
        int(u.get("cache_creation_input_tokens") or 0),
    )


def parse_claude_stream(path: Path) -> AgentMetrics:
    """Aggregate Claude Code's ``claude-code.txt`` stream-json (type=result, total_cost_usd)."""
    out = AgentMetrics()
    if not path.exists():
        return out
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        t = ev.get("type")
        if t == "result":
            out.result_events += 1
            cost = ev.get("total_cost_usd", ev.get("total_cost"))
            if cost is not None:
                out.reported_cost_usd = (out.reported_cost_usd or 0.0) + float(cost)
                out.cost_source = "claude_code"
            u = ev.get("usage")
            if isinstance(u, dict):
                out.input_tokens = 0
                out.output_tokens = 0
                out.cache_read_tokens = 0
                out.cache_write_tokens = 0
                out.has_usage = False
                i, o, cr, cw = _claude_usage_fields(u)
                out.has_usage = True
                out.input_tokens, out.output_tokens = i, o
                out.cache_read_tokens, out.cache_write_tokens = cr, cw
        elif t == "assistant":
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else ev
            u = msg.get("usage") if isinstance(msg, dict) else None
            if isinstance(u, dict) and not out.result_events:
                i, o, cr, cw = _claude_usage_fields(u)
                out.has_usage = True
                out.input_tokens, out.output_tokens = i, o
                out.cache_read_tokens, out.cache_write_tokens = cr, cw
            if isinstance(msg, dict) and msg.get("model"):
                out.model = msg["model"]
            elif isinstance(ev, dict) and ev.get("model"):
                out.model = ev["model"]
    return out


def parse_claude_sessions(agent_dir: Path) -> AgentMetrics:
    """Walk ``agent/sessions/projects/**/*.jsonl`` and take the last accumulated usage per session."""
    out = AgentMetrics()
    root = agent_dir / "sessions" / "projects"
    if not root.exists():
        root = agent_dir / "sessions"
        if not root.exists():
            return out
    files = [p for p in root.rglob("*.jsonl") if p.is_file() and "rollout-" not in p.name]
    if not files:
        return out
    last_usage_by_msg: dict[str, dict[str, Any]] = {}
    tool_calls = 0
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
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else ev
            if not isinstance(msg, dict):
                continue
            mid = ev.get("uuid") or ev.get("id") or msg.get("id")
            usage = msg.get("usage") or ev.get("usage")
            if mid and isinstance(usage, dict):
                last_usage_by_msg[str(mid)] = usage
            typ = ev.get("type") or msg.get("type")
            if typ in {"tool_use", "tool_result"} or msg.get("role") == "tool":
                tool_calls += typ == "tool_use" or (
                    isinstance(msg.get("content"), list)
                    and any(isinstance(p, dict) and p.get("type") == "tool_use" for p in msg["content"])
                )
            content = msg.get("content")
            if typ == "tool_result" or (isinstance(content, str) and ev.get("type") == "user"):
                if isinstance(content, str):
                    tool_chars += len(content)
            if ev.get("cwd") is None and ev.get("type") == "assistant":
                model = msg.get("model")
                if model:
                    out.model = model
    if last_usage_by_msg:

        def _score(u: dict[str, Any]) -> int:
            i, o, cr, cw = _claude_usage_fields(u)
            return i + o + cr + cw

        best = max(last_usage_by_msg.values(), key=_score)
        i, o, cr, cw = _claude_usage_fields(best)
        out.has_usage = True
        out.input_tokens, out.output_tokens = i, o
        out.cache_read_tokens, out.cache_write_tokens = cr, cw
    out.n_tool_calls = tool_calls
    out.tool_output_chars = tool_chars
    out.n_turns = len(last_usage_by_msg) or out.n_turns
    return out


def _prefer(a: AgentMetrics, b: AgentMetrics) -> AgentMetrics:
    if a.has_usage:
        return a
    if b.has_usage:
        return b
    if a.reported_cost_usd is not None or a.n_tool_calls or a.result_events:
        return a
    if b.reported_cost_usd is not None or b.n_tool_calls or b.result_events:
        return b
    return a


class ClaudeCodePlugin(AgentPlugin):
    name = "claude-code"
    billed_cost_sources = frozenset({"claude_code"})

    def collect(self, agent_dir: Path) -> AgentMetrics:
        agent_dir = Path(agent_dir)
        stream = parse_claude_stream(agent_dir / STREAM)
        if stream.has_usage:
            return stream
        return _prefer(stream, parse_claude_sessions(agent_dir))
