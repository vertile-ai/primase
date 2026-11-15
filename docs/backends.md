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
