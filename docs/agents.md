# Writing an agent plugin

A primase agent plugin teaches the harness two things: how to pass kwargs into
Harbor/Pier (`--ak key=value`), and how to collect normalized metrics from the
files the agent left under `agent/`.

Core never branches on a product name. Built-ins (`cursor-cli`, `claude-code`,
`codex`, `noop`) and third-party plugins share the same interface.

## Interface

```python
from pathlib import Path
from typing import Any
from primase.agents import AgentPlugin, AgentMetrics, BudgetHook


class MyAgentPlugin(AgentPlugin):
    name = "my-agent"
    billed_cost_sources = frozenset({"my_agent"})  # cost_source values that mean "CLI billed"

    def harbor_kwargs(self, kwargs: dict[str, Any]) -> dict[str, str]:
        """Map experiment kwargs onto Harbor/Pier --ak strings. Default json-encodes non-strings."""
        return super().harbor_kwargs(kwargs)

    def collect(self, agent_dir: Path) -> AgentMetrics:
        """Parse agent_dir into normalized metrics. Missing files → empty metrics."""
        ...

    def budget_hooks(self, plugin_config: dict[str, Any] | None = None) -> list[BudgetHook]:
        """Optional product-specific spend guards (e.g. Cursor included_models)."""
        return []
```

For the `local` backend, also implement:

```python
from primase.agents import LocalCommand, LocalInvocation

    def local_command(self, inv: LocalInvocation) -> LocalCommand | None:
        # inv.instruction, inv.model, inv.workspace (cwd), inv.agent_dir, inv.kwargs, inv.mcp_config
        return LocalCommand(argv=["my-agent", "-p", inv.instruction], stdout_file="my-agent.txt")
```

`collect()` then parses `agent/<stdout_file>` exactly as it would a Harbor run.

`AgentMetrics` fields: `input/output/cache_read/cache_write` tokens, `reported_cost_usd`,
`cost_source`, `n_turns`, `n_tool_calls`, tool/shell output sizes, `wall_time_s`,
`model`, `agent_version`, and `extra` (e.g. `tool_calls_by_name` — shell calls are labelled by
program, `Bash:agent-browser`, MCP calls by tool name — and `tool_result_images`).

## Registration

```toml
[project.entry-points."primase.agents"]
my-agent = "mypkg.agent:MyAgentPlugin"
```

In tests: `primase.agents.register_agent(MyAgentPlugin())`.

## Experiment YAML

```yaml
agent:
  name: my-agent
  kwargs:
    config: configs/default.json   # becomes --ak config=configs/default.json
  plugin:                          # opaque; interpreted by the plugin
    included_models: []            # cursor-cli only

variants:
  with-alt-config:
    agent:
      kwargs: {config: configs/alt.json}
  with-other-agent:
    agent: {name: claude-code}
    model: {id: claude-opus, provider_prefix: anthropic}
```

## Cursor-specific budget (plugin, not core)

```yaml
agent:
  name: cursor-cli
  plugin:
    included_models: [composer-2.5, "grok-4.7-*"]
    allow_non_included_models: false
budget:
  stop_on_billed_cost: true        # generic: any agent CLI-reported cost > 0
  max_reported_cost_usd: 0
  max_estimated_cost_usd: 50
```

Legacy `budget.included_models` / `budget.stop_on_credit_billing` still load: they
are migrated into `agent.plugin` / `budget.stop_on_billed_cost` automatically.

## Example: noop

The built-in `noop` plugin reads `agent/noop.json` and is the reference for a
minimal third-party agent. See `primase.agents.noop.NoopPlugin`.
