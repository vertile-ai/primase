# Minimal experiment sketch

This directory is an illustration of a primase 0.2 experiment YAML. There is no
task checkout here — copy the shape into your benchmark repository.

```bash
# from a real benchmark root that has Harbor installed:
primase plan     path/to/experiment.yaml
primase validate path/to/experiment.yaml
primase export   path/to/experiment.yaml -o results/demo/bundle
primase verify   results/demo/bundle --config path/to/experiment.yaml
```

See [docs/config.md](../../docs/config.md) and [docs/agents.md](../../docs/agents.md).
