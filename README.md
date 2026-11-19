# primase

A Harbor extension with one job: **measure token savings without giving up correctness.**
Primase sits **on top of** [Harbor](https://github.com/harbor-framework/harbor) (and, optionally,
[Pier](https://github.com/datacurve-ai/pier)): you keep writing tasks in those formats, and
primase runs a baseline and any number of variants and answers two questions, in this order:

1. **Correctness gate** — did the variant hold the baseline's pass rate? If not, it is a
   REGRESSION and its token numbers are not credited.
2. **Token delta** (the headline) — on tasks both arms pass, how many tokens (input / cache /
   output), turns, tool calls, and how much time did the variant save? Median per-task delta
   with a bootstrap 95% CI.

A "variant" is anything you want to price: an agent runtime, an agent config, a model, a
prompt, a context-compression layer, or a **tool** (e.g. which browser CLI or MCP server the
agent drives — see [docs/tools.md](docs/tools.md)).

Primase is an **extension of Harbor**, not a competitor: it adds the comparison,
budget, secrets, and reproducibility layer Harbor does not provide, and stays
project-agnostic.

The import name, CLI, and PyPI distribution name are all `primase`.
This package is not published to PyPI yet; install from Git.

```bash
pip install "primase[harbor] @ git+https://github.com/vertile-ai/primase.git"
# Harbor is the primary backend. The [harbor]/[pier] extras declare the backend;
# you still need the Harbor (or Pier) CLI on PATH / in your benchmark venv.
```

Requires Python 3.11–3.13.

## What primase adds over Harbor and Pier

| | Harbor / Pier alone | Primase |
|---|---|---|
| Backends | One runner, one CLI | One interface over Harbor (primary), Pier (optional), `local` (host, for tools that can't be containerized), plus pluggable entry points |
| Agents | Product-specific flags/logs | Uniform agent plugins (`cursor-cli`, `claude-code`, `codex`, `noop`, …) for invoke + metrics |
| Experiment | Flags assembled by hand | One YAML: models, **general variants** (agent/model/task.toml/env/instruction/MCP servers/files), tiers, trials, budget, network |
| Results | Per-job `result.json` / trajectories | Normalized schema 2.0: pass/fail, tokens, cost, timings, turns, tool calls, **provenance** |
| Comparison | You write the spreadsheet | Verdict per variant: correctness gate first, then median token delta + bootstrap CI; absolute medians; tool-call breakdown |
| Budget | — | Generic billed-cost / estimate caps; product allow-lists live on the agent plugin |
| Secrets | Easy to leak into logs | Env / chmod-600 env-file only; redacted logs; scrubbed job dirs; redacted export bundles |
| Resume | Re-run the job | Deterministic trial IDs; completed jobs skipped |
| Publish | — | `primase export` → self-contained redacted bundle; `primase verify` re-checks it |

## Quickstart

```bash
pip install -e ".[dev]"

primase plan     experiments/my-exp.yaml
primase validate experiments/my-exp.yaml
primase run      experiments/my-exp.yaml --variant control --max-trials 1
primase report   experiments/my-exp.yaml   # → results/<name>/{trials.csv,summary.md}
primase export   experiments/my-exp.yaml -o results/my-exp/bundle
primase verify   results/my-exp/bundle --config experiments/my-exp.yaml
primase scan-secrets --env-file ~/.config/mybench/secrets.env --prefix sk- .
```

Filters `--suite/--task/--model/--variant/--agent/--trial` work on every command.
`--dry-run` prints the exact Harbor/Pier argv; `--force` re-runs a finished trial.

## Experiment config (sketch)

See [docs/config.md](docs/config.md) for the full schema. Unknown keys are rejected.

```yaml
name: demo
root: ..
secrets: [CURSOR_API_KEY]
env_file: ~/.config/mybench/secrets.env
agent:
  name: cursor-cli
  kwargs: {}
  plugin:
    included_models: [composer-2.5, "grok-4.7-*"]
models:
  - {id: composer-2.5, label: Composer 2.5}
variants:
  control:
    description: baseline
    dir: control
  treatment:
    description: experimental runtime
    dir: treatment
    env: { NODE_OPTIONS: "--require /opt/extra/preload.js" }
    require_paths: [environment/extra-layer]
  config-b:                                      # same agent, different config file
    agent: {kwargs: {config: configs/b.json}}
  other-agent:                                   # different agent + model
    agent: {name: claude-code}
    model: {id: claude-opus, provider_prefix: anthropic}
  long-timeout:                                  # task.toml overrides
    task_toml: {agent: {timeout_sec: 600}}
baseline: control
tiers: [low]
suites:
  - {id: my-suite, backend: harbor}
trials: 3
concurrency: 1
network: open
budget:
  stop_on_billed_cost: true
  max_reported_cost_usd: 0
  max_estimated_cost_usd: 50
```

**Variants** are general override bundles (agent, model, task.toml, env, hooks),
not hard-coded A/B arms. The report treats `baseline` as the comparison side and
pairs every other variant against it.

**Migration from 0.1:** `budget.included_models` and `budget.stop_on_credit_billing`
still load (moved to `agent.plugin` / `stop_on_billed_cost`). Existing
env-only variants (e.g. agentic-node `control`/`agentic`) need no changes.

## Architecture

```text
primase plan|validate|run|collect|report|export|verify|scan-secrets
        │
        ├── config.py         YAML → ExperimentConfig (general variants)
        ├── agents/           uniform plugins: invoke kwargs + collect metrics + budget hooks
        ├── registry.py       suites.json → tasks, tiers, variant dirs
        ├── orchestrator.py   plan → run (resume, concurrency, ledger, provenance) → collect
        ├── backends/         Harbor (primary), Pier (optional), local (host)
        ├── local_runner.py   one host trial: workspace, agent CLI, verifier → Harbor-shaped result.json
        ├── collect.py        job dir → TrialResult (delegates to agent plugins)
        ├── budget.py         generic billed-cost + estimate caps
        ├── report.py         verdict (gate → token delta) + N-arm CSV/summary.md (bootstrap CI)
        ├── provenance.py     versions, hashes, digests, artifact paths
        ├── export.py         redacted publishable bundle + verify
        └── secrets.py        env / env-file only; redactor + scrub_tree
```

- [docs/config.md](docs/config.md) — config schema + migration
- [docs/agents.md](docs/agents.md) — third-party agent plugin interface
- [docs/backends.md](docs/backends.md) — third-party backend adapters + the `local` backend
- [docs/tools.md](docs/tools.md) — benchmarking tools (browser CLIs, MCP servers) as variants
- [CONTRIBUTING.md](CONTRIBUTING.md) — development setup

## License

Apache-2.0. Copyright 2026 Vertile AI.
