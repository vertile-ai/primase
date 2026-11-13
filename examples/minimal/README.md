# Minimal experiment

A toy layout showing the YAML primase expects. It is not a real benchmark
(no tasks, no images). Copy the shape into your own benchmark repository.

```text
examples/minimal/
  experiment.yaml
  suites.json
```

`root: .` means "this directory is the benchmark root". In a real repo `root`
usually points at the directory that contains `suites.json` and
`work/variants/`.
