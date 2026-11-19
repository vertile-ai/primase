# Experiment config schema (0.2)

Paths are relative to `root` (the benchmark directory). Unknown keys are rejected
(`extra = forbid`).

## Top-level

| Key | Type | Default | Notes |
|---|---|---|---|
| `name` | slug | required | `a-z0-9._-`, ≤48 chars |
| `description` | str | `""` | |
| `root` | path | `..` | benchmark dir relative to this YAML |
| `jobs_dir` / `results_dir` / `registry` | path | `jobs` / `results` / `suites.json` | |
| `env_file` | path? | | chmod 600; values never logged |
| `secrets` | list[str] | `[]` | **names only** |
| `agent` | AgentSpec | `cursor-cli` | default agent for variants that do not override |
| `models` | list[ModelSpec] | `[]` | crossed with variants that do not set `model` |
| `variants` | map[str, VariantSpec] | required | ≥1 |
| `baseline` | str? | first variant | comparison side for reports |
| `tiers` | list | `[low]` | |
| `suites` | list[SuiteSpec] | required | |
| `trials` / `concurrency` | int | `1` | |
| `network` | `open`\|`restricted` | `open` | overridden by `PRIMASE_NETWORK` |
| `harbor_restricted_compose` | path? | | required when restricted |
| `extra_args` | map[str, list[str]] | | per-backend argv |
| `budget` | BudgetSpec | | generic caps only |

## AgentSpec

```yaml
agent:
  name: cursor-cli          # plugin name (entry point primase.agents)
  kwargs:                   # → Harbor/Pier --ak key=value
    config: path/to.json
  plugin:                   # opaque; interpreted by the agent plugin
    included_models: [composer-2.5, "grok-4.7-*"]   # cursor-cli
    allow_non_included_models: false
```

## VariantSpec — general overrides

A variant is **not** only an environment patch. Any of these may be set:

```yaml
variants:
  control:
    description: baseline
    dir: control                      # work/variants/<suite>/control/<task>
  agentic:
    dir: agentic
    env: { NODE_OPTIONS: "--require /opt/extra/preload.js" }
    setup: []                         # shell, cwd = task dir, each trial
    prebuild: []
    require_paths: [environment/extra-layer]
    require_commands: [agent-browser]   # local backend: validate fails if not on PATH
    image_prefix: extra-
  config-b:
    agent:
      kwargs: {config: configs/b.json}   # same agent, different config
  other-agent:
    agent: {name: claude-code}
    model: {id: claude-opus, provider_prefix: anthropic}
  long-timeout:
    task_toml:                           # arbitrary task.toml table.key patches
      agent: {timeout_sec: 600}
      environment: {cpus: 2}
```

Tool / prompt overlays (applied in the same per-trial task overlay; the source task is never modified):

```yaml
variants:
  ego-browser:
    instruction_prepend: "@prompts/ego-browser.md"   # '@' = file relative to root; else literal text
    instruction_append: "Write your final answer to answer.json."
    overlay_files:                                   # {dest in task dir: src relative to root}
      environment/skills/ego-browser: vendor/ego-lite/skills/ego-browser
  playwright-mcp:
    mcp_servers:                                     # Harbor: [[environment.mcp_servers]]; local: --mcp-config
      - {name: playwright, command: npx, args: ["-y", "@playwright/mcp@latest", "--headless", "--isolated"]}
      # - {name: remote, transport: streamable-http, url: "http://127.0.0.1:8931/mcp"}
```

- If `model` is set, that variant is **not** crossed with top-level `models`.
- If `agent` is set, name/kwargs/plugin merge over the experiment-level agent
  (kwargs and plugin are shallow-merged). The agent only changes when the variant sets
  `agent.name` explicitly (0.2.0 silently fell back to `cursor-cli` when only `kwargs` was set).
- `task_toml` overlays are applied in the same per-trial task directory overlay
  Harbor uses for `network: open`.

## BudgetSpec (generic)

| Key | Default | Notes |
|---|---|---|
| `stop_on_billed_cost` | `true` | abort when any agent CLI reports cost > 0 |
| `max_reported_cost_usd` | `0` | sum of CLI-reported costs |
| `max_estimated_cost_usd` | `null` | price-table estimate runaway guard |

Cursor `included_models` lives under `agent.plugin`, not here.

### Migration from 0.1

| 0.1 | 0.2 |
|---|---|
| `budget.included_models` | `agent.plugin.included_models` (auto-migrated) |
| `budget.allow_non_included_models` | `agent.plugin.allow_non_included_models` (auto-migrated) |
| `budget.stop_on_credit_billing` | `budget.stop_on_billed_cost` (auto-migrated) |
| `arms` / `baseline_arm` | `variants` / `baseline` (still accepted) |

Existing agentic-node YAMLs that only set `env` / `dir` / `require_paths` keep working.
To compare agents or agent configs, add `agent` / `model` / `task_toml` on the variant.

## ModelSpec

```yaml
models:
  - {id: composer-2.5, label: Composer 2.5, provider_prefix: cursor}
```

Harness model string = `provider_prefix/id` (or just `id` when prefix is empty).
