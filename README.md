# helicase

A unified, resumable benchmark harness for agent evals. Helicase sits **on top of**
[Harbor](https://github.com/harbor-framework/harbor) (and, optionally,
[Pier](https://github.com/datacurve-ai/pier)): you keep writing tasks in those
formats, and helicase gives you one experiment YAML, one result schema, and a
report that answers *did pass rate hold, and if so, how many tokens and how much
time did a variant save?*

The import name and CLI are `helicase`. The PyPI distribution name is
**`helicase-bench`** (`helicase` was already taken). This package is not
published to PyPI yet; install from Git.

```bash
pip install "helicase-bench[harbor] @ git+https://github.com/vertile-ai/helicase.git"
# Harbor is the primary backend. The [harbor]/[pier] extras declare the backend;
# you still need the Harbor (or Pier) CLI on PATH / in your benchmark venv.
```

Requires Python 3.11–3.13.

## What helicase adds over Harbor and Pier

Harbor and Pier already run a task in a container and score it with the task's
own verifier. Helicase does not replace that. It adds the layer you need when
you are comparing variants (runtimes, prompts, tools, privileges, …) and care
about **token cost** as well as pass rate:

| | Harbor / Pier alone | Helicase |
|---|---|---|
| Backends | One runner, one CLI | One interface over Harbor (primary) and Pier (optional), plus a pluggable entry-point registry for new harnesses |
| Experiment | Flags assembled by hand | One YAML: models, variants, tiers, trials, concurrency, budget, network |
| Results | Per-job `result.json` / trajectories, different shapes | Normalized, versioned row: pass/fail, input/output/cache tokens, cost + source, timings, turns, tool calls, tool/shell output size |
| Comparison | You write the spreadsheet | CSV + Markdown report: regression flag if a variant's pass rate falls; token/time deltas **only** over tasks both sides passed, using medians |
| Usage parsing | Agent-specific logs | Collectors for cursor-cli, Claude Code, and Codex (plus ATIF `trajectory.json`) |
| Budget | — | Stop on the first CLI-reported (billed) cost, an estimate cap, or a model outside `included_models` |
| Secrets | Easy to leak into logs | Secrets only from the environment or a chmod-600 env-file; redacted logs; job dirs scrubbed after each trial |
| Resume | Re-run the job | Deterministic trial IDs; completed jobs are skipped; interrupted jobs are moved aside |
| Network | Task.toml `network_mode` | Experiment-level `network: open or restricted` (default **open**, because we measure token cost and the agent must reach its API), overridable with `HELICASE_NETWORK`. Open mode forces the *agent* phase to `public` without changing a separate verifier's policy |

Helicase is only the harness. Suites, tasks, experiment YAMLs, and results live
in the benchmark repository that depends on it.

## Quickstart

```bash
# 1. Install helicase into the same venv that already has the Harbor CLI
pip install -e ".[dev]"

# 2. Write an experiment (see examples/minimal/)
helicase plan     experiments/my-exp.yaml
helicase validate experiments/my-exp.yaml
helicase run      experiments/my-exp.yaml --variant control --max-trials 1
helicase report   experiments/my-exp.yaml   # → results/<name>/{trials.csv,summary.md}
helicase scan-secrets --env-file ~/.config/mybench/secrets.env --prefix sk- .
```

Filters `--suite/--task/--model/--variant/--trial` work on every command.
`--dry-run` prints the exact Harbor/Pier argv; `--force` re-runs a finished trial.

## Experiment config

Paths are relative to `root` (the benchmark directory). Unknown keys are rejected.

```yaml
name: demo
root: ..
secrets: [CURSOR_API_KEY]          # names only; values come from the env or env_file
env_file: ~/.config/mybench/secrets.env
agent: {name: cursor-cli}
models:
  - {id: composer-2.5, label: Composer 2.5}
  - {id: grok-4.7-high, label: Grok 4.7 High}
variants:
  control:
    description: baseline
    dir: control                   # work/variants/<suite>/control/<task>
  treatment:
    description: experimental runtime
    dir: treatment
    env: { NODE_OPTIONS: "--require /opt/extra/preload.js" }
    require_paths: [environment/extra-layer]
    image_prefix: extra-           # restore a retained docker tag before run
    setup: []                      # shell, cwd = task dir, before each trial
    prebuild: []
baseline: control
tiers: [low]
suites:
  - {id: my-suite, backend: harbor}   # default backend is harbor
trials: 3
concurrency: 1
network: open                      # default. Override with HELICASE_NETWORK=restricted
# harbor_restricted_compose: adapters/compose-no-egress.yaml   # required when restricted
budget:
  stop_on_credit_billing: true
  max_reported_cost_usd: 0
  max_estimated_cost_usd: 50
  included_models: [composer-2.5, "grok-4.7-*"]
```

**Network.** Default is `open`. `HELICASE_NETWORK=open|restricted` overrides the
YAML when set. In `open` mode Harbor runs against a per-trial overlay that sets
`[agent].network_mode = "public"` (and opens a `no-network` agent-environment
baseline). A separate verifier environment keeps the task's own grading policy.
In `restricted` mode you must point `harbor_restricted_compose` at a compose
overlay that isolates the agent; Pier uses its own egress controls.

**Variants** are generic conditions (env, hooks, prebuild, required paths), not
hard-coded A/B arms. The report treats `baseline` as the comparison side.

## Architecture

```text
helicase plan|validate|run|collect|report|scan-secrets
        │
        ├── config.py        YAML → pydantic ExperimentConfig
        ├── registry.py      suites.json → tasks, tiers, variant dirs
        ├── orchestrator.py  plan → run (resume, concurrency, ledger) → collect
        ├── backends/        Harbor (primary), Pier (optional extra)
        │                    entry points: helicase.backends
        ├── collect.py       job dir → TrialResult (cursor-cli / claude-code / codex / ATIF)
        ├── budget.py        billed-cost + estimate caps
        ├── report.py        trials.csv + summary.md
        └── secrets.py       env / env-file only; redactor + scrub_tree
```

See [docs/backends.md](docs/backends.md) for writing a third-party backend.
See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup.

## License

Apache-2.0. Copyright 2026 Vertile AI.
