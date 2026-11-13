# Changelog

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
