"""Plan → validate → run (resumable) → collect. One harness job per trial, deterministic job names."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from .backends import RunContext, Validation, get_backend
from .budget import BudgetExceeded, BudgetGuard
from .config import ExperimentConfig, VariantSpec
from .registry import Registry
from .schema import TrialResult, TrialSpec
from .secrets import SecretStore

log = logging.getLogger("primase")


@dataclass
class PlannedTrial:
    spec: TrialSpec
    ctx: RunContext
    variant: VariantSpec


def plan(cfg: ExperimentConfig, environ: dict[str, str] | None = None) -> tuple[list[PlannedTrial], list[str]]:
    reg = Registry(cfg.path(cfg.registry))
    planned: list[PlannedTrial] = []
    notes: list[str] = []
    network = cfg.resolved_network(environ)
    compose = cfg.harbor_restricted_compose
    if compose is not None and not compose.is_absolute():
        compose = cfg.path(compose)
    for s in cfg.suites:
        suite = reg.suite(s.id)
        backend_name = s.backend or suite.get("runner", "pier")
        sel, skipped = reg.select(s.id, list(cfg.tiers), s.tasks)
        for t in skipped:
            notes.append(f"skip {s.id}/{t.task}: tier {t.tier} not in {cfg.tiers}")
        for trial in range(1, cfg.trials + 1):
            for t in sel:
                for m in cfg.models:
                    for vname, v in cfg.variants.items():
                        vdir = v.directory_name(vname)
                        spec = TrialSpec(
                            experiment=cfg.name,
                            suite=s.id,
                            task=t.task,
                            tier=t.tier,
                            backend=backend_name,
                            model=m.id,
                            variant=vname,
                            variant_dir=vdir,
                            trial_index=trial,
                            agent=cfg.agent.name,
                        )
                        ctx = RunContext(
                            root=cfg.root_dir,
                            jobs_dir=cfg.jobs_path,
                            task_dir=reg.variant_dir(s.id, vdir, t.task),
                            model_harness_name=m.harness_name,
                            agent=cfg.agent.name,
                            agent_kwargs=dict(cfg.agent.kwargs),
                            agent_timeout_multiplier=s.agent_timeout_multiplier or cfg.agent_timeout_multiplier,
                            env=cfg.env,
                            network=network,
                            harbor_restricted_compose=compose,
                            extra_args=list(cfg.extra_args.get(backend_name, [])),
                            variant_env=dict(v.env),
                            setup=list(v.setup),
                            prebuild=list(v.prebuild),
                            require_paths=list(v.require_paths),
                            image_prefix=v.image_prefix,
                        )
                        planned.append(PlannedTrial(spec, ctx, v))
    return planned, notes


class Orchestrator:
    def __init__(self, cfg: ExperimentConfig, secrets: SecretStore) -> None:
        self.cfg = cfg
        self.secrets = secrets
        self.redact = secrets.redactor
        self.guard = BudgetGuard(cfg.budget)
        self.out = cfg.results_path
        self.out.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def _ledger(self, event: dict[str, object]) -> None:
        with self._lock, (self.out / "ledger.jsonl").open("a") as f:
            f.write(self.redact(json.dumps(event, default=str)) + "\n")

    def _env(self, variant_env: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.secrets.env())
        if variant_env:
            env.update(variant_env)
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def validate(self, trials: list[PlannedTrial]) -> list[tuple[PlannedTrial, Validation]]:
        out = []
        for p in trials:
            b = get_backend(p.spec.backend, p.ctx.root)
            out.append((p, b.validate(p.spec, p.ctx)))
        return out

    def run_one(self, p: PlannedTrial, force: bool = False) -> list[TrialResult]:
        b = get_backend(p.spec.backend, p.ctx.root)
        job = b.job_dir(p.spec, p.ctx)
        if b.is_complete(p.spec, p.ctx) and not force:
            log.info("resume: %s already complete – skipping", p.spec.job_name)
            return b.collect(p.spec, p.ctx)
        if self._stop.is_set():
            return []
        self.guard.check_model(p.spec.model)
        b.prepare_run(p.spec, p.ctx)
        cmd = b.build_command(p.spec, p.ctx)
        if any(self.redact.contains_secret(c) for c in cmd):
            raise RuntimeError("refusing to run: a secret value ended up in argv")
        if any(self.redact.contains_secret(v) for v in p.ctx.variant_env.values()):
            raise RuntimeError("refusing to run: a secret value ended up in variant env (put secrets in env_file)")
        logs = self.out / "logs"
        logs.mkdir(exist_ok=True)
        log.info("run %s: %s", p.spec.job_name, " ".join(cmd))
        self._ledger({"event": "start", "trial_id": p.spec.trial_id, "job": p.spec.job_name, "cmd": cmd})
        with (logs / f"{p.spec.job_name}.log").open("w") as lf:
            proc = subprocess.Popen(
                cmd,
                cwd=p.ctx.root,
                env=self._env(p.ctx.variant_env),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
            assert proc.stdout
            for line in proc.stdout:
                lf.write(self.redact(line))
            rc = proc.wait()
        scrubbed = self.redact.scrub_tree(job) if job.exists() else []
        rows = [r.model_copy(update={"secret_files_scrubbed": len(scrubbed)}) for r in b.collect(p.spec, p.ctx)]
        self._ledger(
            {
                "event": "end",
                "trial_id": p.spec.trial_id,
                "job": p.spec.job_name,
                "rc": rc,
                "scrubbed_files": [str(s) for s in scrubbed],
                "results": [r.model_dump(mode="json") for r in rows],
            }
        )
        if not rows:
            rows = [
                TrialResult(
                    job_name=p.spec.job_name,
                    backend=b.name,
                    task=p.spec.task,
                    status="missing",
                    experiment=p.spec.experiment,
                    trial_id=p.spec.trial_id,
                    suite=p.spec.suite,
                    tier=p.spec.tier,
                    model=p.spec.model,
                    variant=p.spec.variant,
                    variant_dir=p.spec.variant_dir,
                    trial_index=p.spec.trial_index,
                    agent=p.spec.agent,
                    exception=f"runner exit {rc}",
                )
            ]
        return rows

    def run(self, trials: list[PlannedTrial], force: bool = False) -> tuple[list[TrialResult], str | None]:
        missing = self.secrets.missing()
        if missing:
            raise RuntimeError(f"missing secrets: {missing} (export them or set env_file)")
        results: list[TrialResult] = []
        abort: str | None = None
        with ThreadPoolExecutor(max_workers=self.cfg.concurrency) as pool:
            futs = {pool.submit(self.run_one, p, force): p for p in trials}
            for fut in as_completed(futs):
                try:
                    rows = fut.result()
                except Exception as e:  # noqa: BLE001
                    log.error("trial %s crashed: %s", futs[fut].spec.job_name, self.redact(str(e)))
                    continue
                results.extend(rows)
                try:
                    for r in rows:
                        self.guard.observe(r)
                except BudgetExceeded as e:
                    abort = str(e)
                    self._stop.set()
                    self._ledger({"event": "budget_abort", "reason": abort})
                    log.error("BUDGET GUARD: %s", abort)
                    for f in futs:
                        f.cancel()
        return results, abort

    def collect_all(self, trials: list[PlannedTrial]) -> list[TrialResult]:
        rows: list[TrialResult] = []
        for p in trials:
            rows.extend(get_backend(p.spec.backend, p.ctx.root).collect(p.spec, p.ctx))
        return rows
