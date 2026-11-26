# Browser tools as variants

Shape of an experiment that prices browser tools for agents. The curated benchmark
(tasks, fixtures, prompts, provenance) is kept in its own repo, `browser-token-bench`.

```bash
primase plan     examples/browser-tools/experiment.yaml
primase validate examples/browser-tools/experiment.yaml   # checks CLIs on PATH, task layout
primase run      examples/browser-tools/experiment.yaml --max-trials 1
primase report   examples/browser-tools/experiment.yaml
```

See [docs/tools.md](../../docs/tools.md).
