# Changelog

## 0.3.0 – unreleased

Primase's headline is now explicit: **correctness gate first, then token savings.** This release
makes *tools* (browser CLIs, MCP servers, skills, prompts) first-class variants and adds a host
backend for tools that cannot run in a container.

### Verdict-first reports

`summary.md` opens with a **Verdict** table per (suite, model, variant): pass rate baseline →
variant, gate (`hold` / `REGRESSION`), paired-task count, median Δ total tokens with bootstrap 95%
CI, Δ prompt / output tokens, Δ turns, Δ agent time, and a verdict (`saves tokens`, `costs
tokens`, `inconclusive`, `no paired tasks`; regressions are reported but not credited). Followed by
absolute medians per variant and a tool-usage breakdown. Deltas now also cover input and
cache-write tokens.

### Tool variants

New `VariantSpec` fields, applied in the per-trial task overlay (source tasks are never touched):
`instruction_prepend` / `instruction_append` (text or `@file`), `mcp_servers` (Harbor
`[[environment.mcp_servers]]`; local backend: `--mcp-config`), `overlay_files`
(`{dest: src}` copies, e.g. a skill or Dockerfile layer). See [docs/tools.md](docs/tools.md).

### `local` backend

`backend: local` runs the agent CLI on the host via `python -m primase.local_runner` and writes a
Harbor-shaped trial dir, so collection, resume, redaction and reports are unchanged. Agent plugins
gain `local_command()` (built in for `claude-code` and `cursor-cli`; any agent via
`agent.kwargs.local_command`). See [docs/backends.md](docs/backends.md#local-backend).

### Metrics

- Claude Code stream-json now yields turns (distinct assistant messages), tool calls, tool-result
  chars, shell calls/output, screenshots returned, CLI version, and `tool_calls_by_name`
  (`Bash:<program>` / MCP tool names). New `TrialResult` fields: `tool_calls_by_name`,
  `tool_result_images` (additive; schema stays 2.0).
- `trials.csv` JSON-encodes dict/list columns; `primase export` reloads them (`row_from_csv`).

### Fixes

- A variant that only set `agent.kwargs` / `agent.plugin` silently switched the agent to
  `cursor-cli` (the `AgentSpec.name` default). The experiment-level agent is now kept unless the
  variant sets `agent.name`.
- `toml_edit` treats `[[array.of.tables]]` headers as table boundaries, so table-key patches never
  land inside an array-of-tables block.
- Harbor overlays: task.toml overrides, network patch, MCP servers and instruction/file overlays
  are folded into a single overlay (`Backend.build_overlay`).

## 0.2.0 – 2026-11-15

General variant overrides, uniform agent plugins, N-arm reports, and publishable
reproducibility bundles.

### Variants are general overrides

A variant may override **agent** (name / kwargs / plugin), **model**, and
arbitrary **task.toml** fields, in addition to env / setup / prebuild /
require_paths / image_prefix. Comparing stock-vs-agentic runtimes, agent-config
A-vs-B, or agent X-vs-Y all use the same mechanism.

### Agent plugins

Every agent goes through `primase.agents` (`cursor-cli`, `claude-code`, `codex`,
`noop`, plus the `primase.agents` entry-point group). Core no longer branches on
product names. Cursor `included_models` / `allow_non_included_models` live under
`agent.plugin`; core budget keeps only generic caps (`stop_on_billed_cost`,
`max_reported_cost_usd`, `max_estimated_cost_usd`). See [docs/agents.md](docs/agents.md).

### N-arm reports

Every non-baseline variant is paired against the baseline (pass-rate REGRESSION
flag; token/time deltas on tasks both arms pass, medians). Summary tables include
a bootstrap 95% CI on the median of per-task delta%.

### Reproducibility + export

Each trial records primase version/git sha, Harbor/Pier version, agent
name+version, model id, config/variant/task hashes, docker image + digest,
network mode, timestamps, and paths to trajectory / agent log / verifier output
(`TrialResult` schema **2.0**).

New commands:

- `primase export <config> [-o bundle/]` — redacted, self-contained publishable bundle
  (manifest + trials + report + trajectories)
- `primase verify <bundle> [--config experiment.yaml]` — hash + optional config re-check

### Breaking / migration

- `budget.stop_on_credit_billing` → `budget.stop_on_billed_cost` (old key still accepted)
- `budget.included_models` → `agent.plugin.included_models` (old key still accepted)
- `TrialResult.schema_version` is now `"2.0"` (new provenance fields)
- Collectors live under `primase.agents.*`; `primase.collect.parse_*` remain as
  dict-shaped compatibility wrappers

Existing experiment YAMLs that only use env/dir/hooks keep working unchanged.

## 0.1.1 – 2026-11-13

Renamed the project from helicase to **primase**. The import, CLI, and
distribution name are now `primase`; the environment variable is `PRIMASE_NETWORK`
(formerly `HELICASE_NETWORK`). GitHub keeps a redirect from `vertile-ai/helicase`.

## 0.1.0 – 2026-11-13

Initial public release.

- Unified CLI (`primase plan|validate|run|collect|report|scan-secrets`) over Harbor and Pier.
- Harbor is the primary built-in backend; Pier is an optional extra (`primase[pier]`).
- Generic variants (env, setup/prebuild hooks, required paths, image prefix) with a named baseline.
- Versioned `TrialResult` schema and collectors for cursor-cli, Claude Code, and Codex usage.
- Budget guard, secret redaction / job-dir scrub, deterministic resumable trial IDs.
- Reports: pass-rate regression flag; token/time deltas only over both-pass tasks, using medians.
- `network: open|restricted` (default open), overridable with `PRIMASE_NETWORK`. Open mode
  forces the Harbor agent phase to `public` without changing a separate verifier policy.
