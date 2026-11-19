"""Backend abstraction: one interface over Harbor, Pier, and third-party adapters."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..agents import get_agent
from ..collect import collect_job, job_complete
from ..registry import read_task_toml
from ..schema import TrialResult, TrialSpec
from ..toml_edit import append_array_tables, apply_overrides

NetworkMode = Literal["open", "restricted"]


@dataclass
class RunContext:
    """Everything a backend needs that is not part of the TrialSpec."""

    root: Path
    jobs_dir: Path
    task_dir: Path
    model_harness_name: str
    agent: str
    agent_kwargs: dict[str, Any] = field(default_factory=dict)
    agent_timeout_multiplier: float | None = None
    env: str = "docker"
    network: NetworkMode = "open"
    harbor_restricted_compose: Path | None = None
    extra_args: list[str] = field(default_factory=list)
    variant_env: dict[str, str] = field(default_factory=dict)
    setup: list[str] = field(default_factory=list)
    prebuild: list[str] = field(default_factory=list)
    require_paths: list[str] = field(default_factory=list)
    require_commands: list[str] = field(default_factory=list)
    image_prefix: str | None = None
    task_toml_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    instruction_prepend: str | None = None
    instruction_append: str | None = None
    overlay_files: dict[str, str] = field(default_factory=dict)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    model_id: str | None = None

    def has_content_overlay(self) -> bool:
        """True when the variant changes task files beyond task.toml table keys."""
        return bool(self.instruction_prepend or self.instruction_append or self.overlay_files or self.mcp_servers)

    def resolve_text(self, value: str | None) -> str | None:
        """``@path`` → file contents (relative to root); anything else is literal text."""
        if not value:
            return None
        if value.startswith("@"):
            path = Path(value[1:]).expanduser()
            return (path if path.is_absolute() else self.root / path).read_text()
        return value


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
            f"agent_timeout={agent_to}s x{mult or 1} = {round((agent_to or 0) * (mult or 1))}s "
            f"agent={ctx.agent}"
        )
        if ctx.task_toml_overrides:
            msgs.append(f"task_toml overrides: {list(ctx.task_toml_overrides)}")
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
        rows = collect_job(self.job_dir(spec, ctx), self.name, preferred_agent=spec.agent)
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
        """Hook before launching: move incomplete jobs aside, run prebuild/setup, restore images, overlays."""
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
        overlay = self.build_overlay(spec, ctx)
        if overlay is not None:
            ctx.task_dir = overlay

    def build_overlay(self, spec: TrialSpec, ctx: RunContext) -> Path | None:
        """Per-trial task overlay for this backend (``None`` = run the task dir as-is)."""
        if not ctx.task_toml_overrides and not ctx.has_content_overlay():
            return None
        return self._task_overlay(spec, ctx, ctx.task_toml_overrides)

    def _task_overlay(
        self,
        spec: TrialSpec,
        ctx: RunContext,
        overrides: dict[str, dict[str, Any]],
        extra_mutate: Any | None = None,
    ) -> Path | None:
        """Create a per-trial task dir overlay (symlinks for untouched files).

        Applies, in order: task.toml table-key patches, ``extra_mutate`` (e.g. network policy),
        ``[[environment.mcp_servers]]`` blocks, instruction prepend/append, and ``overlay_files``.
        Returns ``None`` when nothing would change.
        """
        src = ctx.task_dir
        toml_path = src / "task.toml"
        if not toml_path.exists():
            return None
        original = toml_path.read_text()
        patched, notes = apply_overrides(original, overrides)
        if extra_mutate is not None:
            patched, extra_notes = extra_mutate(patched)
            notes = list(notes) + list(extra_notes)
        if ctx.mcp_servers and self.renders_mcp_into_task_toml:
            harbor_keys = ("name", "transport", "command", "args", "url")
            entries = [{k: s.get(k) for k in harbor_keys} for s in ctx.mcp_servers]
            patched, mcp_notes = append_array_tables(patched, "environment.mcp_servers", entries)
            notes = list(notes) + mcp_notes
        instruction: str | None = None
        pre, post = ctx.resolve_text(ctx.instruction_prepend), ctx.resolve_text(ctx.instruction_append)
        if pre or post:
            base = (src / "instruction.md").read_text() if (src / "instruction.md").exists() else ""
            parts = [p.rstrip("\n") for p in (pre, base, post) if p]
            instruction = "\n\n".join(parts) + "\n"
        if not notes and patched == original and instruction is None and not ctx.overlay_files:
            return None
        dest = ctx.jobs_dir / ".overlays" / spec.job_name
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        for child in src.iterdir():
            if child.name == "task.toml" or (instruction is not None and child.name == "instruction.md"):
                continue
            os.symlink(child.resolve(), dest / child.name)
        (dest / "task.toml").write_text(patched)
        if instruction is not None:
            (dest / "instruction.md").write_text(instruction)
        for rel_dest, rel_src in ctx.overlay_files.items():
            self._copy_into_overlay(dest, rel_dest, ctx.root / Path(rel_src).expanduser())
        return dest

    #: Harbor reads ``[[environment.mcp_servers]]``; backends that hand MCP config to the CLI set False.
    renders_mcp_into_task_toml: bool = True

    @staticmethod
    def _copy_into_overlay(dest: Path, rel_dest: str, src: Path) -> None:
        rel = Path(rel_dest)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"overlay_files destination must be relative inside the task dir: {rel_dest!r}")
        if not src.exists():
            raise FileNotFoundError(f"overlay_files source missing: {src}")
        # materialize any symlinked parent dirs so we never write through into the source task
        cur = dest
        for part in rel.parts[:-1]:
            cur = cur / part
            if cur.is_symlink():
                target = cur.resolve()
                cur.unlink()
                shutil.copytree(target, cur, symlinks=True)
            cur.mkdir(exist_ok=True)
        target_path = dest / rel
        if target_path.is_symlink() or target_path.is_file():
            target_path.unlink()
        elif target_path.is_dir():
            shutil.rmtree(target_path)
        if src.is_dir():
            shutil.copytree(src, target_path, symlinks=True)
        else:
            shutil.copy2(src, target_path)

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
        try:
            kwargs = get_agent(ctx.agent).harbor_kwargs(ctx.agent_kwargs)
        except KeyError:
            kwargs = {k: (v if isinstance(v, str) else json.dumps(v)) for k, v in ctx.agent_kwargs.items()}
        args: list[str] = []
        for k, v in kwargs.items():
            args += [kw_flag, f"{k}={v}"]
        return args
