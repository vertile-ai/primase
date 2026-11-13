# Writing a backend adapter

A helicase backend is a subclass of `helicase.backends.Backend` that turns one
`TrialSpec` + `RunContext` into a command the orchestrator can run, then
collects a Harbor-like job directory into `TrialResult` rows.

## Interface

```python
from pathlib import Path
from helicase.backends import Backend, RunContext, Validation
from helicase.schema import TrialSpec, TrialResult

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
`image_prefix`, and the resolved `network` mode (`open` or `restricted`). Use
them; do not hard-code a particular product or benchmark.

The default `collect()` assumes the Harbor/Pier trial layout (`result.json`,
`agent/cursor-cli.txt`, `agent/claude-code.txt`, `agent/sessions/**`,
`agent/trajectory.json`). If your runner writes something else, override
`collect()` and/or add a parser in `helicase.collect`.

## Registration

Advertise the class under the `helicase.backends` entry-point group so users
can `pip install` your package next to helicase:

```toml
[project.entry-points."helicase.backends"]
mybench = "mypkg.backend:MyBackend"
```

In tests you can also call `helicase.backends.register_backend("mybench", MyBackend)`.

The experiment YAML then says:

```yaml
suites:
  - {id: some-suite, backend: mybench}
```

If `backend` is omitted, helicase uses the suite's `runner` in `suites.json`,
and if that is omitted too, it defaults to **`harbor`**.

## Built-in backends

- **Harbor** (primary). `helicase-bench[harbor]`. Used for Terminal-Bench and
  any other Harbor-format suite, including DeepSWE (schema 1.3 tasks run on
  Harbor 0.25 / schema 1.4). When `network=open`, Harbor is pointed at a
  per-trial overlay that sets `[agent].network_mode = "public"`; a separate
  verifier environment keeps the task's grading policy.
- **Pier** (optional). `helicase-bench[pier]`. Kept for callers who still need
  Datacurve Pier; new experiments should prefer Harbor. The extras do not
  pip-install the CLIs — install Harbor or Pier into the benchmark venv
  yourself (the PyPI names `harbor` / `pier` are unrelated packages).
