"""Example third-party-style agent plugin: a no-op CLI that writes ``noop.json``.

Demonstrates the minimum surface a custom agent needs. Harbor invokes it as
``-a noop``; primase collects metrics from ``agent/noop.json`` if present.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import AgentMetrics, AgentPlugin

ARTIFACT = "noop.json"


class NoopPlugin(AgentPlugin):
    name = "noop"
    billed_cost_sources = frozenset({"noop"})

    def harbor_kwargs(self, kwargs: dict[str, Any]) -> dict[str, str]:
        # noop accepts a free-form ``config`` path the same way real agents do
        return super().harbor_kwargs(kwargs)

    def collect(self, agent_dir: Path) -> AgentMetrics:
        path = Path(agent_dir) / ARTIFACT
        out = AgentMetrics()
        if not path.exists():
            return out
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return out
        if not isinstance(data, dict):
            return out
        raw_usage = data.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else data
        if any(k in usage for k in ("input_tokens", "inputTokens", "output_tokens", "outputTokens")):
            out.has_usage = True
            out.input_tokens = int(usage.get("input_tokens", usage.get("inputTokens", 0)) or 0)
            out.output_tokens = int(usage.get("output_tokens", usage.get("outputTokens", 0)) or 0)
            out.cache_read_tokens = int(usage.get("cache_read_tokens", usage.get("cacheReadTokens", 0)) or 0)
            out.cache_write_tokens = int(usage.get("cache_write_tokens", usage.get("cacheWriteTokens", 0)) or 0)
        if data.get("cost_usd") is not None:
            out.reported_cost_usd = float(data["cost_usd"])
            out.cost_source = "noop"
        out.n_turns = data.get("n_turns")
        out.n_tool_calls = data.get("n_tool_calls")
        out.wall_time_s = data.get("wall_time_s")
        out.model = data.get("model")
        out.agent_version = data.get("version")
        return out
