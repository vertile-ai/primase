from __future__ import annotations

from ..schema import TrialSpec
from .base import Backend, RunContext


class PierBackend(Backend):
    """Datacurve Pier (`pier run`). Official DeepSWE runner.

    Pier's own egress proxy is used regardless of primase's network setting; when
    network=restricted the experiment should also set a task-level no-network policy
    in the suite (that lives in the benchmark repo, not in primase).
    """

    name = "pier"

    def build_command(self, spec: TrialSpec, ctx: RunContext) -> list[str]:
        cmd = [
            str(self.binary),
            "run",
            "--env",
            ctx.env,
            "-p",
            str(ctx.task_dir),
            "--agent",
            ctx.agent,
            "--model",
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
        if ctx.agent_timeout_multiplier:
            cmd += ["--agent-timeout-multiplier", str(ctx.agent_timeout_multiplier)]
        cmd += self._agent_args(ctx, "--ak")
        return cmd + list(ctx.extra_args)

    def prepare_run(self, spec: TrialSpec, ctx: RunContext) -> None:
        super().prepare_run(spec, ctx)
