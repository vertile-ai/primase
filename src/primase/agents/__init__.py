"""Pluggable agent registry.

Built-in plugins (cursor-cli, claude-code, codex, noop) are always importable.
Third-party packages register via the ``primase.agents`` entry-point group::

    [project.entry-points."primase.agents"]
    my-agent = "mypkg.agent:MyAgentPlugin"
"""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

from .base import AgentMetrics, AgentPlugin, BudgetHook
from .claude_code import ClaudeCodePlugin, parse_claude_sessions, parse_claude_stream
from .codex import CodexPlugin, parse_codex_rollouts
from .cursor_cli import CursorCliPlugin, parse_cursor_stream, tool_output
from .noop import NoopPlugin

_REGISTRY: dict[str, AgentPlugin] | None = None


def _builtins() -> dict[str, AgentPlugin]:
    return {
        "cursor-cli": CursorCliPlugin(),
        "claude-code": ClaudeCodePlugin(),
        "codex": CodexPlugin(),
        "noop": NoopPlugin(),
    }


def _entry_point_agents() -> dict[str, AgentPlugin]:
    found: dict[str, AgentPlugin] = {}
    try:
        eps = entry_points(group="primase.agents")
    except Exception:  # noqa: BLE001
        return found
    for ep in eps:
        try:
            obj = ep.load()
            plugin = obj() if isinstance(obj, type) else obj
            found[plugin.name] = plugin
        except Exception:  # noqa: BLE001
            continue
    return found


def get_registry() -> dict[str, AgentPlugin]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = {**_builtins(), **_entry_point_agents()}
    return _REGISTRY


def register_agent(plugin: AgentPlugin) -> None:
    """Programmatic registration (tests, in-process plugins)."""
    get_registry()[plugin.name] = plugin


def reset_registry() -> None:
    global _REGISTRY
    _REGISTRY = None


def list_agents() -> list[str]:
    return sorted(get_registry())


def get_agent(name: str) -> AgentPlugin:
    try:
        return get_registry()[name]
    except KeyError:
        raise KeyError(f"unknown agent {name!r} (known: {', '.join(list_agents())})") from None


def billed_cost_sources() -> frozenset[str]:
    sources: set[str] = set()
    for p in get_registry().values():
        sources |= set(p.billed_cost_sources)
    return frozenset(sources)


def collect_agent_metrics(agent_dir: Path, preferred: str | None = None) -> AgentMetrics:
    """Collect metrics, preferring *preferred* agent then falling back across plugins."""
    agent_dir = Path(agent_dir)
    order: list[AgentPlugin] = []
    reg = get_registry()
    if preferred and preferred in reg:
        order.append(reg[preferred])
    for name, plugin in reg.items():
        if preferred and name == preferred:
            continue
        order.append(plugin)
    best = AgentMetrics()
    for plugin in order:
        m = plugin.collect(agent_dir)
        if m.has_usage or m.reported_cost_usd is not None or m.n_tool_calls or m.result_events:
            if m.has_usage or not best.has_usage:
                best = m
                if m.has_usage:
                    return best
    return best


def budget_hooks_for(agent_name: str, plugin_config: dict | None = None) -> list[BudgetHook]:
    try:
        return get_agent(agent_name).budget_hooks(plugin_config)
    except KeyError:
        return []


__all__ = [
    "AgentMetrics",
    "AgentPlugin",
    "BudgetHook",
    "budget_hooks_for",
    "billed_cost_sources",
    "collect_agent_metrics",
    "get_agent",
    "get_registry",
    "list_agents",
    "parse_claude_sessions",
    "parse_claude_stream",
    "parse_codex_rollouts",
    "parse_cursor_stream",
    "register_agent",
    "reset_registry",
    "tool_output",
]
