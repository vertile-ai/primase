"""Local (host) backend: run the agent CLI directly on this machine, no container.

For tools that cannot live in a Linux container — e.g. a macOS-only GUI browser that
agents drive through a CLI — or for quick iteration without Docker. Tasks keep the
Harbor layout (``task.toml``, ``instruction.md``, ``tests/test.sh``, ``environment/``);
see docs/backends.md#local for the conventions. The trial itself is executed by
``python -m primase.local_runner <spec.json>`` so the orchestrator's logging, secret
redaction, resume, and collection paths are identical to Harbor's.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from ..agents import LocalInvocation, get_agent
from ..registry import read_task_toml
from ..schema import TrialSpec
from .base import Backend, RunContext, Validation


class LocalBackend(Backend):
    name = "local"
    renders_mcp_into_task_toml = False

    @property
    def binary(self) -> Path:
        return Path(sys.executable)

    def spec_path(self, spec: TrialSpec, ctx: RunContext) -> Path:
        return ctx.jobs_dir / ".local-specs" / f"{spec.job_name}.json"

    def trial_dir(self, spec: TrialSpec, ctx: RunContext) -> Path:
        return self.job_dir(spec, ctx) / f"{spec.task}__{spec.trial_id}"

    def validate(self, spec: TrialSpec, ctx: RunContext) -> Validation:
        msgs: list[str] = []
        ok = True
        td = ctx.task_dir
        for rel in ("task.toml", "instruction.md", "tests/test.sh"):
            if not (td / rel).exists():
                ok = False
                msgs.append(f"missing {rel}")
        if ctx.network == "restricted":
            ok = False
            msgs.append("network=restricted is not enforceable on the local backend (host network)")
        try:
            plugin = get_agent(ctx.agent)
            probe = plugin.local_command(
                LocalInvocation(
                    instruction="x", model=ctx.model_id, workspace=td, agent_dir=td, kwargs=ctx.agent_kwargs
                )
            )
            if probe is None:
                ok = False
                msgs.append(f"agent {ctx.agent!r} has no local_command (set agent.kwargs.local_command)")
            elif shutil.which(probe.argv[0]) is None and not Path(probe.argv[0]).exists():
                ok = False
                msgs.append(f"agent binary not found on PATH: {probe.argv[0]}")
        except KeyError as e:
            ok = False
            msgs.append(str(e))
        timeout = None
        if (td / "task.toml").exists():
            try:
                timeout = (read_task_toml(td).get("agent") or {}).get("timeout_sec")
            except Exception as e:  # noqa: BLE001
                ok = False
                msgs.append(f"task.toml parse error: {e}")
        for rel in ctx.require_paths:
            if not (td / rel).exists():
                ok = False
                msgs.append(f"variant requires missing path {rel}")
        msgs.append(
            f"local agent={ctx.agent} model={ctx.model_id} timeout={timeout}s x{ctx.agent_timeout_multiplier or 1} "
            f"mcp={[s.get('name') for s in ctx.mcp_servers]} overlay={ctx.has_content_overlay()}"
        )
        return Validation(ok, str(td), msgs)

    def prepare_run(self, spec: TrialSpec, ctx: RunContext) -> None:
        super().prepare_run(spec, ctx)
        path = self.spec_path(spec, ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "task": spec.task,
            "task_dir": str(ctx.task_dir),
            "trial_dir": str(self.trial_dir(spec, ctx)),
            "agent": ctx.agent,
            "agent_kwargs": ctx.agent_kwargs,
            "model": ctx.model_id,
            "mcp_servers": ctx.mcp_servers,
            "agent_timeout_multiplier": ctx.agent_timeout_multiplier,
        }
        path.write_text(json.dumps(payload, indent=2, default=str))

    def build_command(self, spec: TrialSpec, ctx: RunContext) -> list[str]:
        return [str(self.binary), "-m", "primase.local_runner", str(self.spec_path(spec, ctx))]
