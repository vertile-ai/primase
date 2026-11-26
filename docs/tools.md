# Benchmarking tools as variants

Primase's question is "does this change save tokens **without** losing correctness?". A tool the
agent uses — a browser CLI, an MCP server, a code-search tool, an output compressor — is just
another variant. Hold the agent, model, and tasks fixed; vary only the tool; read the verdict.

## What a tool variant can change

| Need | Variant field |
|---|---|
| Tell the agent which tool to use / how | `instruction_prepend` / `instruction_append` (text or `@file`) |
| Expose an MCP server | `mcp_servers` (Harbor `[[environment.mcp_servers]]`; local: `--mcp-config`) |
| Ship a skill, AGENTS.md, Dockerfile layer, wrapper script | `overlay_files` |
| Fail `validate` early when the tool's CLI is missing on the host (local backend) | `require_commands` |
| Install / start the tool | `prebuild` (once) / `setup` (per trial) hooks, or the task's Dockerfile |
| Tool flags / env | `env`, `agent.kwargs` (e.g. `allowed_tools`, `disallowed_tools`, `max_turns`) |
| Tools that cannot run in a Linux container | `backend: local` on the suite (see [backends.md](backends.md#local-backend)) |

The fields are not all honoured by every backend; `validate` fails when a variant sets one its
backend ignores. See [backends.md](backends.md#what-local-does-not-support).

Keep the arms fair: every variant should get an instruction of similar length and the same
answer format; block shortcuts that bypass the tool under test (e.g. `disallowed_tools:
[WebFetch, WebSearch]` for browser tools). Arm order rotates per task and trial, so no arm
always runs first.

## Tools installed as agent configuration

Many token-saving tools are not prompt-driven: they are a hook, a settings file or a wrapper on
`PATH` (an output compressor such as rtk, for example). Do not describe them in
`instruction_prepend` — that changes the prompt and confounds the comparison. Ship the
configuration into the agent's workspace instead:

```yaml
variants:
  rtk-off:
    dir: shared
  rtk-on:
    dir: shared
    require_commands: [rtk]                      # fails validate if missing; version is recorded
    overlay_files:                               # local backend: must land under environment/workspace/
      environment/workspace/.claude/settings.json: configs/rtk-settings.json
baseline: rtk-off
suites:
  - {id: my-suite, backend: local}
```

On the `local` backend `claude-code` ignores your user-level configuration, so hooks and
instructions you have installed globally are off in every arm unless the variant ships them.
The tool's binary is still on `PATH` in every arm.

## What gets measured

Per trial (`trials.csv`): pass/fail + reward, input / output / cache-read / cache-write tokens,
total tokens, CLI-reported cost, turns, tool calls, **`tool_calls_by_name`** (shell calls split by
program, e.g. `Bash:agent-browser`, `Bash:ego-browser`; MCP calls by tool, e.g.
`mcp__playwright__browser_snapshot`), tool-result chars (≈ tokens/4, text only — images are
counted in `tool_result_images` but not as tokens), agent wall time, total wall time,
`tool_versions` (`<cmd> --version` for each `require_commands` entry).

Token fields have one meaning for every agent: `input_tokens` is non-cached input, cache read
and write are separate, and all are cumulative over the trial. `total_tokens` is their
unweighted sum plus output, so it is dominated by cache reads; read it together with the cost
column. Token numbers are comparable between variants of the same agent and model. The report
prints a note when a comparison crosses agents.

## How the verdict is decided

`summary.md` leads with one **Verdict** row per (suite, model, variant).

**1. Correctness gate.** Computed over the (task, trial) cells that exist in both arms with a
pass/fail result. Trials that errored (runner crash, setup failure, verifier timeout) are counted
separately, not as failures.

| Gate | Meaning |
|---|---|
| `hold` | mean per-task pass-rate difference (variant − baseline) is ≥ 0 |
| `REGRESSION` | the difference is negative and its bootstrap 95% CI is entirely below 0 |
| `unclear` | negative but within noise, more than 10% of either arm's trials errored, or no common cells |

Token savings are credited only on `hold`. Below the table the report lists the tasks the
variant lost and the tasks whose baseline is itself unstable across trials.

**2. Token delta.** Over tasks where both arms have at least one usable passing trial. A trial
is usable when it passed, did not time out, and reported its final usage; a killed agent's usage
is a lower bound (`usage_partial`) and is excluded. Per task, the delta is between the arms'
medians. The headline is the median per-task Δ total tokens with a 95% CI from a two-level
bootstrap (tasks, then trials within each task), next to the aggregate Δ (sum of tokens over the
paired tasks), Δ cost, and how many tasks were dropped from pairing and by which arm.

| Verdict | Meaning |
|---|---|
| `saves tokens` / `costs tokens` | gate holds, ≥ 6 paired tasks, CI entirely below / above 0 |
| `inconclusive` | gate holds, ≥ 6 paired tasks, CI includes 0 |
| `inconclusive (n=k paired tasks, need 6)` | fewer than 6 paired tasks; no direction is claimed |
| `no paired tasks` | no task has a usable passing trial in both arms |
| `REGRESSION` / `gate unclear` | token delta shown but not credited |

Six is the smallest number of tasks at which "every task moved the same way" is unlikely by
chance (sign test, p < 0.05). Plan for at least that many tasks and 3 trials.

## Example

[`examples/browser-tools/experiment.yaml`](../examples/browser-tools/experiment.yaml) compares
agent-browser (baseline), ego-browser, Playwright MCP, and Chrome DevTools MCP with Claude Code on
the `local` backend. A full curated task set lives in the separate `browser-token-bench` repo.
