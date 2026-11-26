# Design review: `feat/browser-tool-variants` (0.3.0.dev0)

Scope: the 4 commits on top of `fc930e5`. Question asked: does this generalize as a
"tokens saved at equal correctness" tool, or is it shaped by the browser benchmark?

Tests: `.venv/bin/python -m pytest -q` → **61 passed**. Nothing below is a failing test; the
issues are in what the tests do not cover (measurement validity, arm fairness, statistics).

Items marked *(verified)* were reproduced by running the code; the rest come from reading it.
Items marked *(pre-existing)* predate this branch but matter more now.

## Status (follow-up)

All blockers and should-fix items below were addressed after this review, in their smallest
form, and checked with unit tests (82 passed) plus a real 8-trial Claude Code run on the `local`
backend. The current behaviour is documented in `docs/backends.md` and `docs/tools.md`; the text
below describes the code as it was at review time.

Deliberately not done:

- B2: `env_file` secrets still reach the agent process; there is no sandbox.
- S1: unstable baseline tasks are listed, not excluded from the gate.
- S5: the verdict metric is still `total_tokens`; cost is shown beside it.
- S8: the shell wrapper list in `first_program()` is still fixed (`rtk git status` → `Bash:rtk`).
- S10: no new schema field; the `overlay_files` recipe is documented in `docs/tools.md`.
- N6, N7.

Found during the real run, not fixed: a killed trial reports no cost, so it is invisible to the
budget cap; the default budget aborts a `claude-code` run after its first trial; each local
trial leaves a temp workspace and a session directory under `~/.claude/projects`.

## Summary

The variant / backend / agent-plugin split is sound and the verdict-first report is the right
shape. The branch is not yet trustworthy as a general measuring instrument, for three reasons:

1. **Arms are not isolated from the host on the `local` backend**, and the isolation differs
   between arms (B1, B2). This directly breaks an rtk on/off comparison on this machine.
2. **Token numbers can be silently wrong** for timed-out trials (B3) and are not comparable
   across agents (S6).
3. **The verdict can say "saves tokens" or "REGRESSION" on evidence that does not support it**
   (B4, B5, S1–S3).

Browser-specific leakage into core is small (S8). Claude/Cursor-specific leakage is larger
(S7), and it is what will bite first when adding a third agent.

---

## Blockers

### B1. `local` backend: the host's agent config leaks into every trial, asymmetrically

`src/primase/agents/claude_code.py:266-267`, `src/primase/local_runner.py:139,193-202`

- `--strict-mcp-config` is passed only when the variant defines `mcp_servers`. An MCP arm sees
  only its own servers; a non-MCP arm (e.g. `agent-browser`, the example baseline) also loads
  every user/project-level MCP server configured on the host, and pays for their tool
  definitions in the prompt. The arms differ by more than the tool under test.
- Nothing isolates user-level config: `~/.claude/CLAUDE.md`, settings, hooks, skills, plugins
  and memory are loaded in all arms. The agent also inherits the full host environment.
- The workspace lives at `<jobs_dir>/<job>/<trial>/workspace`, normally inside a repository, so
  project-level instruction/config discovery can walk up into the enclosing repo.

Why it blocks the stated goal: on this machine the global instructions tell the agent to prefix
shell commands with `rtk`. An "rtk off" control arm would still use rtk. The same applies to any
tool that is installed globally on the host.

Suggestion: make host isolation the plugin's job and symmetric across arms — always pass a
strict MCP config (an empty one when the variant has no servers), run with a per-trial or
explicitly chosen agent config/home, and record what was loaded in provenance. Exact CLI flags
need checking against the current Claude Code and cursor-agent versions. Put the workspace
outside any repository (a real temp dir) or document that `jobs_dir` must be.

### B2. `local` backend: the agent can read the grader and write its own reward

`src/primase/local_runner.py:139-150,198`

The agent process receives `PRIMASE_TASK_DIR`, `PRIMASE_VERIFIER_DIR`, `PRIMASE_STATE_DIR` and
`PRIMASE_TRIAL_DIR` in its environment, plus every secret from `env_file`
(`orchestrator.py:125-131`). `tests/test.sh`, expected answers and fixtures are one `cat` away,
and `reward.txt` is writable at `../verifier/`. The docstring claim at
`local_runner.py:102-104` ("answers cannot be read off disk") does not hold.

`bypassPermissions` does not confine the agent to the workspace, and the workspace is not a
temp dir. `docs/backends.md:112-114` says "only point it at tasks you trust", but the risk is
the agent (and whatever web content it reads in a browser task), not the task.

For a tool whose headline is a correctness gate, the gate must not be reachable by the agent.
Minimum: do not export task/verifier/state paths or unrelated secrets to the agent process
(keep them for `local-setup.sh` and `test.sh`); keep `verifier/` and the task dir outside the
agent's cwd ancestry. Stronger confinement (sandbox profile, separate user) is optional and
should be justified by a reproduced problem.

### B3. Timed-out Claude trials report last-turn usage as if it were the trial total *(verified)*

`src/primase/agents/claude_code.py:120-127`, `src/primase/collect.py:162-216`,
`src/primase/report.py:149-151`

Without a `result` event, the parser keeps the `usage` of the last `assistant` message. That
is one API call, not the cumulative total. `has_usage` becomes true, so `usage_missing` is
false and the report's "no usage" exclusion never fires. Reproduced with a two-message stream
and no `result` event: the row reported 12 / 50 / 25,000 / 300 tokens, i.e. only the second
message.

The local runner still runs the verifier after an agent timeout (`local_runner.py:205-235`),
so such a trial can pass and enter the token delta with a large underestimate — biased toward
whichever arm times out more. The background section of the task description says timeouts
"cannot get final usage"; the code does not treat them that way.

Suggestion: when there is no `result` event, either sum per-message usage deduplicated by
message id and mark the row as partial, or set `usage_missing`. Either way exclude
`agent_timed_out` rows from token deltas by default, and show the count per arm in the verdict
table.

### B4. The correctness gate counts infrastructure failures as task failures, in both directions

`src/primase/report.py:106-133`, `src/primase/orchestrator.py:212-231`

`pass_rates` uses every row as the denominator, including `status="error"` and `"missing"`
(runner crash, `local-setup.sh` failure, verifier timeout, rate limit).

- Errors in the variant arm → false `REGRESSION`.
- Errors in the **baseline** arm lower the bar, so a worse variant reads `hold`.

Also, the rate is pooled per (suite, model, variant) over whatever rows exist. After a partial
run (`--max-trials`, budget abort, resume) the two arms can cover different tasks, and the
comparison is no longer like-for-like.

Suggestion: compute the gate only over (task, trial) cells present in both arms; report
errors separately; withhold the verdict when either arm's error rate exceeds a threshold.

### B5. "saves tokens" is reachable with two tasks *(verified)*

`src/primase/report.py:79-103,219-229`

The verdict requires `n_paired_tasks >= 2` and a bootstrap CI entirely below zero. A percentile
bootstrap of the median over n points never leaves the observed range, so whenever all paired
deltas are negative the CI is below zero. Two tasks at −3% and −40% give CI [−40, −3] →
`saves tokens`. The equivalent sign test has p = 0.5 at n = 2 and needs n ≥ 6 for p < 0.05.

Suggestion: require a minimum number of paired tasks before any directional verdict (6 is the
sign-test floor; state the number in the report), otherwise `inconclusive (n too small)`.

---

## Should fix

### S1. The gate has no notion of noise; flaky tasks decide the verdict

`src/primase/report.py:115-133`

`regression = variant_rate < baseline_rate` on a strict inequality. With the example's 3 trials
over a handful of tasks, one flaky failure flips the verdict, and one flaky failure in the
baseline hides a real one. Equal pooled rates also pass the gate when the variant fails
*different* tasks than the baseline.

Suggestion: (a) a per-task view — list tasks the baseline passes and the variant does not;
(b) a non-inferiority margin with an interval on the paired pass-rate difference, with three
outcomes (hold / regression / cannot tell) instead of two; (c) flag tasks whose baseline is
itself unstable across trials and exclude them from the gate.

### S2. The CI ignores trial-level variance

`src/primase/report.py:136-196`

Each task is reduced to one number (median of passing trials per arm) and only tasks are
resampled. Run-to-run variance of agent token usage is large, and with `trials: 1` the
per-task delta is a single noisy pair. A task with 1/3 passing in one arm and 3/3 in the other
is paired at equal weight. Suggestion: resample trials within task as well (two-level
bootstrap), and show trials per arm next to each paired task.

### S3. The bootstrap seed is not deterministic across runs *(verified)*

`src/primase/report.py:184,217`

`hash((model, variant, metric))` depends on `PYTHONHASHSEED`; two invocations gave
2400654246 and 3352427524. The module docstring and footnote say "deterministic". Regenerating
`summary.md` from the same CSV can change CI bounds and, near zero, the verdict. Use a stable
digest (e.g. `zlib.crc32` of the joined key).

### S4. Median of per-task percentages is the only headline; it is not what a user pays

`src/primase/report.py:199-231`

A tool that saves 50% on small tasks and costs 10% on large ones reads as a saving. Add the
aggregate ratio (sum of tokens over paired tasks, variant / baseline) beside the median.

Survivorship: deltas cover only tasks both arms passed. That is a defensible rule, but a
variant that fails the expensive tasks looks cheaper. Print how many tasks were dropped and by
which arm.

### S5. `total_tokens` is unweighted and dominated by cache reads

`src/primase/collect.py:177,229`, `src/primase/report.py:215-229`

`total = input + cache_read + cache_write + output`. For agentic CLIs cache reads are most of
the total and are billed at a fraction of input price, so the headline tracks
"turns × context size" rather than cost. `cost_usd` deltas are computed in `verdicts()` but
never shown in the verdict table. Suggestion: show cost (or a price-weighted token count)
beside raw tokens and let the config pick the verdict metric (`verdicts()` hard-codes
`total_tokens`).

Order effect on cost: the plan order is fixed (`orchestrator.py:47-53`: trial → task → variant
→ model), so the baseline always runs first and pays the prompt-cache write that later arms
read. Rotate or shuffle arm order per task.

### S6. Token fields do not mean the same thing across agents

`src/primase/agents/codex.py:19,63-67` *(pre-existing)*, `cursor_cli.py:91-104`,
`claude_code.py:103-119`

- Claude: one cumulative `result.usage`; `input_tokens` excludes cached tokens.
- Cursor: `result` usages are summed.
- Codex: reads `last_token_usage`, which is the last turn rather than the session total (the
  docstring says "cumulative"), and Codex's `input_tokens` already includes cached input, so
  `prompt_tokens_total = input + cache_read` double counts. Check against a real rollout file.

Same-agent, same-model A/B is fine. "Agent X vs agent Y" — which `VariantSpec` advertises — is
not. Suggestion: define the canonical meaning of each `AgentMetrics` field in
`agents/base.py`, make each plugin normalize to it, and have the report warn when a comparison
crosses agents or models.

Related: when a variant overrides `model`, `regressions()` finds no baseline row under that
model and the variant disappears from the verdict table without a message
(`report.py:119`).

### S7. Claude/Cursor specifics in the core runner

`src/primase/local_runner.py:70-81,151-157`

- `mcp_config()` renders the Claude/Cursor `mcpServers` JSON in the runner. Codex uses a
  different format.
- `.cursor/mcp.json` is written into the workspace for every agent. A Claude trial gets a
  stray `.cursor/` dir that the agent can see and that a verifier diffing the workspace will
  trip over.

`agents/base.py:5` says "Core never branches on a product name". Suggestion: let the plugin
own MCP delivery — `local_command()` receives the server list and returns the files to write
(or the plugin gets a `prepare_local(inv)` hook). That removes both items from the runner.

Smaller instances: `provenance.py:128,161-166` hard-codes agent log names and branches on
`backend == "harbor"/"pier"`; `config.py:28,38` default to `cursor` / `cursor-cli`
*(pre-existing)*.

### S8. Browser-shaped pieces in core

- `schema.py:119` `tool_result_images` is a top-level column. Images count as zero in
  `tool_output_tokens_est` (`claude_code.py:73-74`), so for screenshot-returning tools the
  estimate undercounts exactly where the browser arms differ. Either estimate image tokens or
  label the column "text only" in the report.
- `{{PRIMASE_PORT}}` is a single-port, web-fixture convention (`local_runner.py:48-59`). It is
  harmless, but `free_port()` releases the port before use and can collide when
  `concurrency > 1`.
- The "Tool usage" section exists only for Claude: the Cursor and Codex plugins do not emit
  `tool_calls_by_name`.
- `first_program()` (`claude_code.py:38-61`) is a heuristic with a fixed wrapper list. For an
  rtk experiment every call is labelled `Bash:rtk` *(verified)*, and `uv run pytest` is
  labelled `Bash:uv`, so the breakdown stops being informative. Make the wrapper list
  configurable per experiment rather than adding tool names to the plugin.

### S9. Variant fields mean different things per backend, and no-ops are silent

`src/primase/backends/base.py:171-225`, `backends/local.py`, `docs/tools.md:9-17`

| Field | Harbor | local |
|---|---|---|
| `env` | reaches the `harbor` process, not the container | reaches the agent |
| `overlay_files` | lands in the task dir; reaches the agent only if the Dockerfile copies it, never with a prebuilt `docker_image` | reaches the agent only under `environment/workspace/` |
| `task_toml` | all keys | only `[agent].timeout_sec` and `[verifier].timeout_sec` are read |
| `mcp_servers[].env` | dropped | used |
| `require_commands` | ignored | checked |
| `image_prefix`, experiment `env: docker` | used | ignored |

`docs/tools.md` presents these as one uniform table. Suggestion: each backend declares which
variant fields it honours and `validate` fails (or warns loudly) when a variant sets one the
backend will ignore. This is the main thing standing between the schema and "works for the
next tool".

Also: `require_commands` is enforced only by `primase validate`; `primase run` does not
validate first, so the "caught before any paid trial" promise in `config.py:136-138` depends on
the user remembering to run it.

### S10. The schema has no first-class way to say "configure the agent"

Most token-saving tools are installed as agent configuration — a hook, a settings file, an
instructions file, a wrapper on `PATH` — not as a prompt prefix or MCP server. An rtk on/off
experiment today needs: `overlay_files` targeting `environment/workspace/.claude/...` (local
only, path differs on Harbor), a Dockerfile change for Harbor, and B1 fixed so the control arm
is actually clean. `instruction_prepend` works but changes the prompt, which is a confound
when the tool under test is not prompt-driven.

Suggestion, minimal: one variant field for files placed in the agent's workspace/home,
implemented by each backend (local: copy; Harbor: via the mechanism Harbor already provides),
plus the per-backend validation from S9. Do not build a tool-plugin system for this.

### S11. Reproducibility gaps in provenance

`src/primase/provenance.py:73-75,143-175`

- `variant_hash` hashes the variant definition, so `instruction_prepend: "@prompts/x.md"` and
  `overlay_files` sources can change content without changing the hash.
- No version is recorded for the tool under test. The example uses `@playwright/mcp@latest`
  and `chrome-devtools-mcp@latest`; `require_commands` checks presence only.
- On `local`, nothing records the host (OS, agent config in effect) and the Docker fields are
  empty.

Suggestion: hash the resolved prompt and overlay contents; record `<cmd> --version` for each
`require_commands` entry; pin versions in the example.

### S12. State leaks between trials on the host

`src/primase/local_runner.py:193-213,239`

The agent's process group is killed only on timeout. After a normal exit, children it started
(browser daemons, MCP servers, dev servers) survive into the next trial, which may be another
arm. Host-level state (browser profiles, tool caches) is not reset either. Kill the agent's
process group unconditionally after it exits; document that per-trial state reset is the
task's `local-setup.sh` responsibility.

`concurrency > 1` on `local` shares one host between trials. Reject it in `validate` or warn.

---

## Nice to have

- **N1. Harbor ↔ local task portability.** `local.py:4-5` says tasks "keep the Harbor
  layout", but a Harbor `tests/test.sh` writes to a fixed container path and will not find
  `$PRIMASE_VERIFIER_DIR`; a local task needs `local-setup.sh` in place of the Dockerfile. The
  same task cannot run on both backends unchanged, so local results cannot be cross-checked
  against Harbor. Say so in `docs/backends.md`.
- **N2. `prebuild` runs before every trial** (`backends/base.py:161-162`), while
  `config.py:86-88` documents "once before the first trial of this variant" *(pre-existing)*.
  Hooks also run with `shell=True` in the shared task dir and without the variant env.
- **N3. Multiple `result` events**: cost is summed but usage is overwritten
  (`claude_code.py:103-119`). Pick one rule.
- **N4. Timeout kill**: `_kill_group` sends SIGTERM and then SIGKILL after 0.5 s
  (`local_runner.py:84-92`), which gives the CLI little time to flush a final usage event.
  A longer grace period may make B3 rarer; measure before changing.
- **N5. `status="failed"` vs timeout**: the trials table shows `(timeout)`, but the verdict
  table does not show how many passing rows per arm were timeouts or had partial usage.
- **N6. Example fairness**: `examples/browser-tools/experiment.yaml` compares four tools with
  different prompt files; `docs/tools.md:19-21` asks for "similar length" by convention only.
  Reporting the rendered instruction length per arm in `plan` would make it checkable.
- **N7. `collect.py:17-26,101-114`** keeps direct imports of the three agent modules for
  legacy shims *(pre-existing)*; it contradicts the plugin boundary but is cosmetic.

---

## Does it generalize?

**Yes, in structure.** A tool is a variant; the gate-then-delta report asks the right question
for any token-saving tool; `shell_output_tokens_est` is already the right secondary metric for
an output compressor like rtk; the `local_command` escape hatch covers unknown agents.

**Not yet, in practice**, and mostly not because of the browser:

- The `local` backend assumes a clean host and a cooperative agent (B1, B2, S12). The browser
  benchmark tolerated this because its arms differed in an obvious, external tool. A tool that
  lives in agent config will not.
- The statistics were sized for a demo (few tasks, 3 trials) and the verdict wording is
  stronger than the evidence at that size (B4, B5, S1–S3).
- The variant schema reads as backend-neutral but is not (S9, S10).

Suggested order: B3 and S3 (small, local fixes) → B1 and B2 (arm fairness and gate integrity)
→ B4, B5, S1 (verdict rules) → S9 (per-backend field validation). Then run an rtk on/off
experiment as the second real use case before designing anything further; S10 should be shaped
by what that run needs, not in advance.
