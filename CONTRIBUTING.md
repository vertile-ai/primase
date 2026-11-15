# Contributing

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Python 3.11–3.13 are supported. CI runs ruff, mypy, and pytest on all three.

## Checks

```bash
ruff check src tests && ruff format --check src tests
mypy src
pytest -q
```

Please keep changes to the harness generic. Suites, tasks, and experiment YAMLs
belong in the benchmark repository that *depends on* primase, not in this repo
(beyond the tiny `examples/minimal/` illustration).

## Backends and agents

Harbor is the primary built-in backend. Pier is kept as an optional extra for
callers who still need it; new work should prefer Harbor. To add another runner,
see [docs/backends.md](docs/backends.md) and register an entry point in the
`primase.backends` group.

To add another agent CLI, see [docs/agents.md](docs/agents.md) and register an
entry point in the `primase.agents` group. Keep product-specific budget policy
on the plugin (`agent.plugin`), not in core.

## Secrets

Never commit API keys, env files, or job/result artifacts. `primase scan-secrets`
can gate a push. Test fixtures must use obviously fake values.
