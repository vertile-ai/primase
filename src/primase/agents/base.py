"""Uniform agent plugin interface.

Every agent product (cursor-cli, claude-code, codex, custom CLIs) is a plugin that
knows (1) how its kwargs map onto Harbor/Pier ``--ak`` flags and (2) how to collect
normalized metrics from its on-disk output. Core never branches on a product name.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AgentMetrics:
    """Normalized usage / behaviour metrics extracted from one trial's agent/ dir.

    Canonical token fields (every plugin must normalize to these): ``input_tokens`` is NON-cached
    input only; ``cache_read_tokens`` and ``cache_write_tokens`` are counted separately (so
    ``prompt_tokens_total = input + cache_read + cache_write``); all counts are cumulative over the
    whole trial (every model call), never just the last turn.
    """

    has_usage: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reported_cost_usd: float | None = None
    cost_source: str | None = None
    n_turns: int | None = None
    n_tool_calls: int | None = None
    tool_output_chars: int | None = None
    n_shell_calls: int | None = None
    shell_output_chars: int | None = None
    wall_time_s: float | None = None
    model: str | None = None
    agent_version: str | None = None
    result_events: int = 0
    is_error: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def prompt_tokens_total(self) -> int | None:
        if not self.has_usage:
            return None
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    def as_usage_dict(self) -> dict[str, int] | None:
        if not self.has_usage:
            return None
        return {
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "cacheReadTokens": self.cache_read_tokens,
            "cacheWriteTokens": self.cache_write_tokens,
        }


@dataclass
class LocalInvocation:
    """Inputs for running an agent CLI directly on the host (``local`` backend)."""

    instruction: str
    model: str | None
    workspace: Path
    agent_dir: Path
    kwargs: dict[str, Any] = field(default_factory=dict)
    #: the variant's MCP servers (``McpServerSpec`` dicts); the plugin decides how to hand them to its CLI
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class LocalCommand:
    """argv + the file (under ``agent_dir``) that captures stdout, so :meth:`AgentPlugin.collect` can parse it.

    ``files`` (absolute path -> content) are written by the local runner right before launch, so
    :meth:`AgentPlugin.local_command` itself stays side-effect free (validate calls it as a probe).
    """

    argv: list[str]
    stdout_file: str
    env: dict[str, str] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)


def mcp_config(servers: list[dict[str, Any]]) -> dict[str, Any]:
    """Claude Code / Cursor ``mcpServers`` JSON."""
    out: dict[str, Any] = {}
    for s in servers:
        if s.get("transport", "stdio") == "stdio":
            entry: dict[str, Any] = {"command": s["command"], "args": list(s.get("args") or [])}
            if s.get("env"):
                entry["env"] = dict(s["env"])
        else:
            entry = {"type": "http" if s["transport"] == "streamable-http" else "sse", "url": s["url"]}
        out[s["name"]] = entry
    return {"mcpServers": out}


class BudgetHook:
    """Optional agent-specific budget policy. Core BudgetGuard calls every hook."""

    def check_model(self, model_id: str) -> None:
        """Raise ``BudgetExceeded`` if *model_id* is not allowed under this policy."""

    def observe_billed(self, job_name: str, cost_usd: float) -> None:
        """Called when a trial reports a billed (CLI) cost. May raise ``BudgetExceeded``."""


class AgentPlugin(ABC):
    """One agent product.

    Implement :meth:`collect` to parse the agent directory Harbor/Pier left behind.
    Optionally override :meth:`budget_hooks` for product-specific spend guards
    (e.g. Cursor included-usage allow-lists).
    """

    name: str
    #: ``cost_source`` values this plugin emits that mean "the CLI itself billed"
    billed_cost_sources: frozenset[str] = frozenset()

    def harbor_kwargs(self, kwargs: dict[str, Any]) -> dict[str, str]:
        """Map experiment kwargs onto Harbor/Pier ``--ak key=value`` strings."""
        out: dict[str, str] = {}
        for k, v in kwargs.items():
            out[k] = v if isinstance(v, str) else json.dumps(v)
        return out

    @abstractmethod
    def collect(self, agent_dir: Path) -> AgentMetrics:
        """Parse ``agent_dir`` into normalized metrics. Missing files → empty metrics."""

    def local_command(self, inv: LocalInvocation) -> LocalCommand | None:
        """argv to run this agent headless on the host (``local`` backend). ``None`` = unsupported.

        A generic escape hatch for any agent: ``agent.kwargs.local_command`` (list of strings) with
        ``{instruction}``, ``{instruction_file}``, ``{model}``, ``{workspace}``, ``{mcp_config}`` placeholders
        and ``agent.kwargs.local_stdout_file``. ``{mcp_config}`` is ``<agent_dir>/mcp.json``, written (with the
        variant's servers, possibly none) only when the template references it.
        """
        return generic_local_command(inv, default_stdout=f"{self.name}.txt")

    def budget_hooks(self, plugin_config: dict[str, Any] | None = None) -> list[BudgetHook]:
        """Return budget hooks configured by the experiment's ``agent.plugin`` block."""
        return []

    def detect_version(self, agent_dir: Path) -> str | None:
        """Best-effort agent CLI version from on-disk artifacts."""
        return self.collect(agent_dir).agent_version


def generic_local_command(inv: LocalInvocation, default_stdout: str) -> LocalCommand | None:
    template = inv.kwargs.get("local_command")
    if not template:
        return None
    if isinstance(template, str):
        import shlex

        template = shlex.split(template)
    instr_file = inv.agent_dir / "instruction.md"
    mcp_path = inv.agent_dir / "mcp.json"
    values = {
        "instruction": inv.instruction,
        "instruction_file": str(instr_file),
        "model": inv.model or "",
        "workspace": str(inv.workspace),
        "mcp_config": str(mcp_path),
    }
    files: dict[str, str] = {}
    if any("{mcp_config}" in str(part) for part in template):
        files[str(mcp_path)] = json.dumps(mcp_config(inv.mcp_servers), indent=2)
    argv = [str(part).format(**values) for part in template]
    return LocalCommand(argv=argv, stdout_file=str(inv.kwargs.get("local_stdout_file") or default_stdout), files=files)
