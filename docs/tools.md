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
| Install / start the tool | `prebuild` (once) / `setup` (per trial) hooks, or the task's Dockerfile |
| Tool flags / env | `env`, `agent.kwargs` (e.g. `allowed_tools`, `disallowed_tools`, `max_turns`) |
| Tools that cannot run in a Linux container | `backend: local` on the suite (see [backends.md](backends.md#local-backend)) |

Keep the arms fair: every variant should get an instruction of similar length and the same
answer format; block shortcuts that bypass the tool under test (e.g. `disallowed_tools:
[WebFetch, WebSearch]` for browser tools).

## What gets measured

Per trial (`trials.csv`): pass/fail + reward, input / output / cache-read / cache-write tokens,
total tokens, CLI-reported cost, turns, tool calls, **`tool_calls_by_name`** (shell calls split by
program, e.g. `Bash:agent-browser`, `Bash:ego-browser`; MCP calls by tool, e.g.
`mcp__playwright__browser_snapshot`), tool-result chars (≈ tokens/4), screenshots returned,
agent wall time, total wall time.

`summary.md` leads with the **Verdict** table — pass-rate gate first, then the median per-task
Δ total tokens with a bootstrap 95% CI (`saves tokens` / `costs tokens` / `inconclusive` /
`REGRESSION`) — then absolute medians per variant and the tool-usage breakdown.

## Example

[`examples/browser-tools/experiment.yaml`](../examples/browser-tools/experiment.yaml) compares
agent-browser (baseline), ego-browser, Playwright MCP, and Chrome DevTools MCP with Claude Code on
the `local` backend. A full curated task set lives in the separate `browser-token-bench` repo.
