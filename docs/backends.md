# Writing a backend adapter

A primase backend is a subclass of `primase.backends.Backend` that turns one
`TrialSpec` + `RunContext` into a command the orchestrator can run, then
collects a Harbor-like job directory into `TrialResult` rows.

## Interface

```python
from pathlib import Path
from primase.backends import Backend, RunContext, Validation
from primase.schema import TrialSpec, TrialResult


class MyBackend(Backend):
    name = "mybench"

    def build_command(self, spec: TrialSpec, ctx: RunContext) -> list[str]:
        """argv for one trial. Must not contain secret values; secrets travel in the process env."""
        ...

    # Optional overrides (sensible defaults live on Backend):
    #   validate(spec, ctx) -> Validation
    #   prepare_run(spec, ctx) -> None     # e.g. hooks, overlays, restore images
    #   collect(spec, ctx) -> list[TrialResult]
    #   is_complete(spec, ctx) -> bool
```

`RunContext` already carries variant env, setup/prebuild hooks, `require_paths`,
`image_prefix`, `task_toml_overrides`, agent kwargs, and the resolved `network`
mode (`open` or `restricted`). Use them; do not hard-code a particular product
or benchmark.

The default `collect()` assumes the Harbor/Pier trial layout and delegates agent
log parsing to the `primase.agents` plugin registry. If your runner writes
something else, override `collect()` and/or add an agent plugin (see
[agents.md](agents.md)).

## Registration

Advertise the class under the `primase.backends` entry-point group so users
can `pip install` your package next to primase:

```toml
[project.entry-points."primase.backends"]
mybench = "mypkg.backend:MyBackend"
```

In tests you can also call `primase.backends.register_backend("mybench", MyBackend)`.

The experiment YAML then says:

```yaml
suites:
  - {id: some-suite, backend: mybench}
```

If `backend` is omitted, primase uses the suite's `runner` in `suites.json`,
and if that is omitted too, it defaults to **`harbor`**.

## Built-in backends

- **Harbor** (primary). `primase[harbor]`. Used for Terminal-Bench and
  any other Harbor-format suite, including DeepSWE (schema 1.3 tasks run on
  Harbor 0.25 / schema 1.4). When `network=open`, Harbor is pointed at a
  per-trial overlay that sets `[agent].network_mode = "public"`; a separate
  verifier environment keeps the task's grading policy.
- **Pier** (optional). `primase[pier]`. Kept for callers who still need
  Datacurve Pier; new experiments should prefer Harbor. The extras do not
  pip-install the CLIs — install Harbor or Pier into the benchmark venv
  yourself (the PyPI names `harbor` / `pier` are unrelated packages).
- **local** (built in). Runs the agent CLI directly on the host — no container. Use it for
  tools that cannot run in a Linux container (e.g. a macOS-only GUI browser driven through a
  CLI) or for fast iteration. See below.

## `local` backend

```yaml
suites:
  - {id: browser, backend: local}
concurrency: 1          # required: every trial shares this one host
```

Each trial runs `python -m primase.local_runner <spec.json>` (so logging, secret redaction,
resume and collection are the same as for Harbor) and writes a Harbor-shaped trial dir:

```text
<job>/<task>__<trial_id>/
  result.json          started/finished, agent_execution, verifier_result.rewards, exception_info, workspace
  agent/<cli>.txt      agent stdout (claude-code.txt / cursor-cli.txt stream-json) → agent plugin collect()
  agent/instruction.md agent/mcp.json agent/command.json agent/stderr.txt
  verifier/reward.txt  written by tests/test.sh
  state/               $PRIMASE_STATE_DIR — scratch for fixture servers
```

The agent's working directory is **not** in the trial dir. It is a fresh temp dir
(`primase-ws-*` under `$TMPDIR`), seeded from `environment/workspace/`, and its path is recorded
in `result.json` as `workspace`. It is left on disk after the trial for inspection.

Task conventions (Harbor layout, plus):

| Item | Meaning |
|---|---|
| `environment/workspace/` | copied into the agent's cwd; nothing else under `environment/` is |
| `environment/local-setup.sh` | run before the agent (cwd = workspace); may start background servers; its process group is killed after the verifier |
| `{{PRIMASE_PORT}}` in `instruction.md` | replaced by a free TCP port, also exported as `$PRIMASE_PORT` |
| `{{env:NAME}}` in `instruction.md` | replaced by `$NAME` from the runner env (trial errors if unset), e.g. a self-hosted site URL |
| `tests/test.sh` | run after the agent (cwd = workspace); writes `$PRIMASE_VERIFIER_DIR/reward.txt` or `reward.json`; with neither, exit code 0 → reward 1.0 |

Environment variables:

| Process | Gets |
|---|---|
| agent | `PRIMASE_PORT`, `PRIMASE_WORKSPACE` only |
| `local-setup.sh`, `tests/test.sh` | those plus `PRIMASE_TASK_DIR`, `PRIMASE_TRIAL_DIR`, `PRIMASE_AGENT_DIR`, `PRIMASE_VERIFIER_DIR`, `PRIMASE_STATE_DIR` (`PRIMASE_TESTS_DIR` for the verifier) |

The agent command comes from the agent plugin's `local_command()` (built in for `claude-code`
and `cursor-cli`), or from `agent.kwargs.local_command` (a list with `{instruction}`,
`{instruction_file}`, `{model}`, `{workspace}`, `{mcp_config}` placeholders) for any agent.
The plugin also owns MCP delivery: it returns the config files to write, and the runner writes
them. Timeouts come from `task.toml` (`[agent].timeout_sec` × multiplier,
`[verifier].timeout_sec`). On timeout the agent's process group gets SIGTERM, then SIGKILL after
5 s. The group is also killed after a normal exit, so children the agent left behind (browser
daemons, dev servers) do not survive into the next trial.

### Keeping the arms equal

Every arm runs on the same host, so the host must look the same to every arm.

- `claude-code` always runs with `--strict-mcp-config` and its own `agent/mcp.json` (empty when
  the variant has no `mcp_servers`), so no arm sees MCP servers configured on the host.
- `claude-code` runs with `--setting-sources project` by default: user-level settings, hooks,
  plugins, skills and instructions are not loaded. A variant can still ship
  `.claude/settings.json` in the workspace. Override with `agent.kwargs.setting_sources`.
- `cursor-cli` gets `.cursor/mcp.json` in the workspace only when the variant has `mcp_servers`.
- State outside the workspace (browser profiles, tool caches) is not reset by primase. Reset it
  in the task's `local-setup.sh` when the tool under test keeps state.

### What `local` does not support

`validate` (and `run`, which validates first) fails when a variant sets something this backend
would ignore: `image_prefix`, or `task_toml` keys other than `agent.timeout_sec` /
`verifier.timeout_sec`. `network: restricted` and `concurrency > 1` are rejected. The reverse
holds on Harbor/Pier: `require_commands` and `mcp_servers[].env` are local-only and fail there.

Two variant fields mean different things per backend:

| Field | Harbor / Pier | local |
|---|---|---|
| `env` | reaches the `harbor` process, not the container | reaches the agent, setup and verifier |
| `overlay_files` | lands in the task dir; reaches the agent only if the Dockerfile copies it | reaches the agent only under `environment/workspace/` |

A task does not run unchanged on both backends: a Harbor `tests/test.sh` writes its reward to a
fixed container path, and a local task uses `local-setup.sh` where Harbor uses the Dockerfile.

### Safety

`claude-code` runs with `--permission-mode bypassPermissions` by default because the trial is
unattended (override with `agent.kwargs.permission_mode`). This is not a sandbox: the agent runs
as you, with your network and your files. primase keeps the task dir, the verifier dir and
their paths out of the agent's working directory and environment, which stops an agent from
stumbling on the grader, not one that searches the disk for it. Secrets from `env_file` are in
the agent's environment. `network: restricted` is rejected.
