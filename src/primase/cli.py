"""`primase` CLI: plan | validate | run | collect | report | scan-secrets."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .config import ExperimentConfig, load_config
from .orchestrator import Orchestrator, PlannedTrial, plan
from .report import markdown, write_csv
from .schema import TrialResult
from .secrets import Redactor, SecretStore, parse_env_file


class _RedactingFormatter(logging.Formatter):
    def __init__(self, redact: Redactor, *a: object, **kw: object) -> None:
        super().__init__(*a, **kw)  # type: ignore[arg-type]
        self.redact = redact

    def format(self, record: logging.LogRecord) -> str:
        return self.redact(super().format(record))


def _setup_logging(redact: Redactor, verbose: bool) -> None:
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(_RedactingFormatter(redact, "%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    root = logging.getLogger("primase")
    root.handlers[:] = [h]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def _filter(trials: list[PlannedTrial], args: argparse.Namespace) -> list[PlannedTrial]:
    variant = args.variant or getattr(args, "arm", None)

    def ok(item: PlannedTrial) -> bool:
        s = item.spec
        return (
            (not args.suite or s.suite in args.suite.split(","))
            and (not args.task or s.task in args.task.split(","))
            and (not args.model or s.model in args.model.split(","))
            and (not variant or s.variant in variant.split(","))
            and (not args.trial or s.trial_index in [int(x) for x in args.trial.split(",")])
        )

    return [item for item in trials if ok(item)]


def _report(cfg: ExperimentConfig, rows: list[TrialResult], notes: list[str] | None = None) -> tuple[Path, Path]:
    out = cfg.results_path
    csv_p = write_csv(rows, out / "trials.csv")
    md_p = out / "summary.md"
    md_p.write_text(markdown(rows, f"primase – {cfg.name}", cfg.baseline_name, notes))
    return csv_p, md_p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="primase", description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "validate", "run", "collect", "report"):
        sp = sub.add_parser(name)
        sp.add_argument("config")
        sp.add_argument("--suite")
        sp.add_argument("--task")
        sp.add_argument("--model")
        sp.add_argument("--variant")
        sp.add_argument("--arm", help="deprecated alias for --variant")
        sp.add_argument("--trial")
        sp.add_argument("-v", "--verbose", action="store_true")
        if name == "run":
            sp.add_argument("--force", action="store_true", help="re-run trials even if complete")
            sp.add_argument("--dry-run", action="store_true", help="print commands only")
            sp.add_argument("--max-trials", type=int, help="hard cap on the number of trials launched")
    sc = sub.add_parser(
        "scan-secrets", help="exit 1 if any secret value (from env/env-file) or prefix occurs under PATHs"
    )
    sc.add_argument("paths", nargs="+")
    sc.add_argument("--env-file")
    sc.add_argument(
        "--names", default="", help="comma-separated secret env names to load (plus every key in --env-file)"
    )
    sc.add_argument("--prefix", action="append", default=[], help="also flag this literal prefix")
    args = ap.parse_args(argv)

    if args.cmd == "scan-secrets":
        vals: dict[str, str] = {}
        if args.env_file:
            vals.update(parse_env_file(args.env_file))
        names = [n for n in args.names.split(",") if n] or list(vals)
        vals.update({k: os.environ[k] for k in names if k in os.environ})
        red = Redactor()
        for k in names:
            red.add(vals.get(k))
        needles = [v.encode() for v in red.values] + [p.encode() for p in args.prefix]
        if not needles:
            print("nothing to scan for (pass --names, --env-file, or --prefix)", file=sys.stderr)
            return 2
        hits: list[str] = []
        for root in args.paths:
            for f in [Path(root)] if Path(root).is_file() else Path(root).rglob("*"):
                if f.is_file() and not f.is_symlink() and ".git" not in f.parts:
                    try:
                        data = f.read_bytes()
                    except OSError:
                        continue
                    if any(n in data for n in needles):
                        hits.append(str(f))
        print(json.dumps({"needles": len(needles), "hits": hits}, indent=2))
        return 1 if hits else 0

    cfg = load_config(args.config)
    secrets = SecretStore.load(cfg.secrets, cfg.env_file_path)
    _setup_logging(secrets.redactor, args.verbose)
    trials, notes = plan(cfg)
    trials = _filter(trials, args)

    if args.cmd == "plan":
        for n in notes:
            print(f"# {n}")
        print(f"# network={cfg.resolved_network()}")
        for p in trials:
            print(
                f"{p.spec.trial_id}  {p.spec.backend:6} {p.spec.suite}/{p.spec.task} [{p.spec.tier}] "
                f"model={p.spec.model} variant={p.spec.variant}(dir={p.spec.variant_dir}) "
                f"t{p.spec.trial_index}  → {p.spec.job_name}"
            )
        print(f"# {len(trials)} trials; secrets: {secrets.describe()}")
        return 0

    orch = Orchestrator(cfg, secrets)
    if args.cmd == "validate":
        bad = 0
        for p, v in orch.validate(trials):
            bad += not v.ok
            print(
                f"{'OK ' if v.ok else 'BAD'} {p.spec.backend:6} {p.spec.suite}/{p.spec.task} "
                f"{p.spec.variant}: {'; '.join(v.messages)}"
            )
        print(f"# {len(trials) - bad}/{len(trials)} valid; secrets: {secrets.describe()}")
        return 1 if bad else 0

    if args.cmd == "run":
        if args.max_trials is not None and len(trials) > args.max_trials:
            print(f"refusing: {len(trials)} trials selected > --max-trials {args.max_trials}", file=sys.stderr)
            return 2
        from .backends import get_backend

        if args.dry_run:
            for p in trials:
                print(" ".join(get_backend(p.spec.backend, p.ctx.root).build_command(p.spec, p.ctx)))
            return 0
        rows, abort = orch.run(trials, force=args.force)
        csv_p, md_p = _report(
            cfg,
            orch.collect_all(_filter(plan(cfg)[0], args)),
            [f"BUDGET ABORT: {abort}"] if abort else None,
        )
        print(f"wrote {csv_p}\nwrote {md_p}")
        return 3 if abort else 0

    rows = orch.collect_all(trials)
    csv_p, md_p = _report(cfg, rows)
    print(f"{len(rows)} trial rows\nwrote {csv_p}\nwrote {md_p}")
    if args.cmd == "report":
        print(md_p.read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
