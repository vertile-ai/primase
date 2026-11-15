# Changelog

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
