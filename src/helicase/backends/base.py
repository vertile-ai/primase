"""Backend abstraction: one interface over Harbor, Pier, and third-party adapters."""

from __future__ import annotations

import json
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..collect import collect_job, job_complete
from ..registry import read_task_toml
from ..schema import TrialResult, TrialSpec

NetworkMode = Literal["open", "restricted"]


@dataclass
class RunContext:
    """Everything a backend needs that is not part of the TrialSpec."""

    root: Path
    jobs_dir: Path
    task_dir: Path
    model_harness_name: str
    agent: str
    agent_kwargs: dict[str, str] = field(default_factory=dict)
    agent_timeout_multiplier: float | None = None
    env: str = "docker"
    network: NetworkMode = "open"
    harbor_restricted_compose: Path | None = None
    extra_args: list[str] = field(default_factory=list)
    variant_env: dict[str, str] = field(default_factory=dict)
    setup: list[str] = field(default_factory=list)
    prebuild: list[str] = field(default_factory=list)
    require_paths: list[str] = field(default_factory=list)
    image_prefix: str | None = None


@dataclass
class Validation:
    ok: bool
    task_dir: str
    messages: list[str] = field(default_factory=list)


class Backend(ABC):
    name: str = "base"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @property
    def binary(self) -> Path:
        return self.root / ".venv" / "bin" / self.name

    @abstractmethod
    def build_command(self, spec: TrialSpec, ctx: RunContext) -> list[str]:
        """argv for one trial. MUST NOT contain secret values (secrets travel via the process env only)."""

    def validate(self, spec: TrialSpec, ctx: RunContext) -> Validation:
        msgs: list[str] = []
        ok = True
        if not self.binary.exists():
            ok = False
            msgs.append(f"missing {self.binary}")
        if not (ctx.task_dir / "task.toml").exists():
            return Validation(False, str(ctx.task_dir), msgs + ["missing task.toml"])
        try:
            toml = read_task_toml(ctx.task_dir)
        except Exception as e:  # noqa: BLE001
            return Validation(False, str(ctx.task_dir), msgs + [f"task.toml parse error: {e}"])
        env = toml.get("environment", {})
        img = env.get("docker_image")
        has_df = (ctx.task_dir / "environment" / "Dockerfile").exists() or (
            ctx.task_dir / "environment" / "docker-compose.yaml"
        ).exists()
        if not img and not has_df:
            ok = False
            msgs.append("no docker_image and no environment/Dockerfile|docker-compose.yaml")
        agent_to = (toml.get("agent") or {}).get("timeout_sec")
        mult = ctx.agent_timeout_multiplier
        msgs.append(
            f"image={img or 'build:environment/'} network={ctx.network} "
            f"task_network={env.get('network_mode', 'default')} "
            f"agent_timeout={agent_to}s x{mult or 1} = {round((agent_to or 0) * (mult or 1))}s"
        )
        for rel in ctx.require_paths:
            if not (ctx.task_dir / rel).exists():
                ok = False
                msgs.append(f"variant requires missing path {rel}")
        return Validation(ok, str(ctx.task_dir), msgs)

    def job_dir(self, spec: TrialSpec, ctx: RunContext) -> Path:
        return ctx.jobs_dir / spec.job_name

    def is_complete(self, spec: TrialSpec, ctx: RunContext) -> bool:
        return job_complete(self.job_dir(spec, ctx))

    def collect(self, spec: TrialSpec, ctx: RunContext) -> list[TrialResult]:
        rows = collect_job(self.job_dir(spec, ctx), self.name)
        out: list[TrialResult] = []
        for r in rows:
            r = r.model_copy(
                update={
                    "experiment": spec.experiment,
                    "trial_id": spec.trial_id,
                    "suite": spec.suite,
                    "tier": spec.tier,
                    "variant": spec.variant,
                    "variant_dir": spec.variant_dir,
                    "trial_index": spec.trial_index,
                    "model": r.model or spec.model,
                    "agent": r.agent or spec.agent,
                    "task": spec.task,
                }
            )
            out.append(r)
        return out

    def run_hooks(self, commands: list[str], cwd: Path) -> None:
        for raw in commands:
            subprocess.run(raw, shell=True, check=True, cwd=cwd)

    def prepare_run(self, spec: TrialSpec, ctx: RunContext) -> None:
        """Hook before launching: move incomplete jobs aside, run prebuild/setup, restore images."""
        job = self.job_dir(spec, ctx)
        if job.exists() and not job_complete(job):
            n = 1
            while (aside := job.with_name(f"{job.name}.incomplete{n}")).exists():
                n += 1
            shutil.move(str(job), str(aside))
        if ctx.prebuild:
            self.run_hooks(ctx.prebuild, ctx.task_dir)
        if ctx.setup:
            self.run_hooks(ctx.setup, ctx.task_dir)
        if ctx.image_prefix:
            self._restore_prebuilt(ctx.task_dir, ctx.image_prefix)

    @staticmethod
    def _restore_prebuilt(task_dir: Path, image_prefix: str) -> None:
        try:
            img = (read_task_toml(task_dir).get("environment") or {}).get("docker_image") or ""
        except Exception:  # noqa: BLE001
            return
        if not img.startswith(image_prefix):
            return
        if subprocess.run(["docker", "image", "inspect", img], capture_output=True).returncode != 0:
            # convention: a retained tag inserts "keep-" after the prefix
            keep = img.replace(image_prefix, f"{image_prefix}keep-", 1)
            subprocess.run(["docker", "tag", keep, img], capture_output=True)

    def _agent_args(self, ctx: RunContext, kw_flag: str) -> list[str]:
        args: list[str] = []
        for k, v in ctx.agent_kwargs.items():
            args += [kw_flag, f"{k}={v if isinstance(v, str) else json.dumps(v)}"]
        return args
