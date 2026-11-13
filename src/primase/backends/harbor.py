from __future__ import annotations

import os
from pathlib import Path

from ..schema import TrialSpec
from ..toml_edit import get_table_key, set_table_key
from .base import Backend, RunContext


def _force_open_agent_network(task_toml: str) -> tuple[str, list[str]]:
    """Rewrite task.toml so the *agent* can reach the public internet.

    Verifier network_mode and a separate verifier environment are left as the
    task defined them. If the agent environment baseline is no-network, it is
    also opened so the container actually has a network at start.
    """
    notes: list[str] = []
    text = task_toml
    agent_mode = get_table_key(text, "agent", "network_mode")
    if agent_mode and agent_mode != "public":
        text = set_table_key(text, "agent", "network_mode", "public")
        notes.append(f"agent.network_mode {agent_mode} -> public")
    elif agent_mode is None:
        text = set_table_key(text, "agent", "network_mode", "public")
        notes.append("agent.network_mode set to public")
    env_mode = get_table_key(text, "environment", "network_mode")
    if env_mode and env_mode != "public":
        text = set_table_key(text, "environment", "network_mode", "public")
        notes.append(f"environment.network_mode {env_mode} -> public (agent env baseline)")
    return text, notes


class HarborBackend(Backend):
    """Harbor (`harbor run`). Primary built-in backend.

    When primase ``network=open`` (the default), a per-trial overlay of the task
    directory is used so ``[agent].network_mode`` is forced to ``public``. A
    separate verifier environment keeps the task's own grading network policy.
    """

    name = "harbor"

    def build_command(self, spec: TrialSpec, ctx: RunContext) -> list[str]:
        cmd = [
            str(self.binary),
            "run",
            "-e",
            ctx.env,
            "-p",
            str(ctx.task_dir),
            "-a",
            ctx.agent,
            "-m",
            ctx.model_harness_name,
            "-k",
            "1",
            "-n",
            "1",
            "--job-name",
            spec.job_name,
            "-o",
            str(ctx.jobs_dir),
            "-y",
            "-q",
        ]
        if ctx.network == "restricted":
            compose = ctx.harbor_restricted_compose
            if compose is None:
                raise ValueError(
                    "network=restricted requires harbor_restricted_compose in the experiment config "
                    "(a docker-compose override that isolates the agent network)"
                )
            cmd += ["--extra-docker-compose", str(compose if compose.is_absolute() else self.root / compose)]
        if ctx.agent_timeout_multiplier:
            cmd += ["--agent-timeout-multiplier", str(ctx.agent_timeout_multiplier)]
        cmd += self._agent_args(ctx, "--ak")
        return cmd + list(ctx.extra_args)

    def prepare_run(self, spec: TrialSpec, ctx: RunContext) -> None:
        super().prepare_run(spec, ctx)
        if ctx.network == "open":
            overlay = self._open_network_overlay(spec, ctx)
            if overlay is not None:
                ctx.task_dir = overlay

    def _open_network_overlay(self, spec: TrialSpec, ctx: RunContext) -> Path | None:
        src = ctx.task_dir
        toml_path = src / "task.toml"
        if not toml_path.exists():
            return None
        original = toml_path.read_text()
        patched, notes = _force_open_agent_network(original)
        if not notes:
            return None
        dest = ctx.jobs_dir / ".overlays" / spec.job_name
        if dest.exists():
            # previous attempt: start clean
            import shutil

            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        for child in src.iterdir():
            if child.name == "task.toml":
                continue
            os.symlink(child, dest / child.name)
        (dest / "task.toml").write_text(patched)
        return dest
