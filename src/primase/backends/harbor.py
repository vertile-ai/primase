from __future__ import annotations

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
    Variant ``task_toml`` overrides are applied in the same overlay.
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
        # Run hooks / image restore without applying task_toml yet — we fold that into one overlay.
        overrides = dict(ctx.task_toml_overrides)
        ctx.task_toml_overrides = {}
        super().prepare_run(spec, ctx)
        ctx.task_toml_overrides = overrides

        need_network = ctx.network == "open"
        if not overrides and not need_network:
            return

        def mutate(text: str) -> tuple[str, list[str]]:
            notes: list[str] = []
            if need_network:
                text, n = _force_open_agent_network(text)
                notes.extend(n)
            return text, notes

        overlay = self._task_overlay(spec, ctx, overrides, extra_mutate=mutate if need_network else None)
        if overlay is not None:
            ctx.task_dir = overlay
        elif need_network:
            # overrides empty but network open with no changes needed — still fine
            pass
