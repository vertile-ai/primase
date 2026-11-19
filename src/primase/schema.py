"""Normalized, versioned result schema shared by every backend."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "2.0"

Status = Literal["passed", "failed", "error", "missing"]


class TrialSpec(BaseModel):
    """One unit of work: (suite, task, model, variant, trial_index).

    The id is deterministic so resume is idempotent.
    """

    model_config = ConfigDict(frozen=True)

    experiment: str
    suite: str
    task: str
    tier: str
    backend: str
    model: str
    variant: str
    variant_dir: str
    trial_index: int
    agent: str

    @property
    def trial_id(self) -> str:
        key = "|".join(
            [
                self.experiment,
                self.suite,
                self.task,
                self.model,
                self.variant,
                self.variant_dir,
                self.agent,
                str(self.trial_index),
            ]
        )
        return hashlib.sha256(key.encode()).hexdigest()[:10]

    @property
    def job_name(self) -> str:
        def slug(s: str) -> str:
            return re.sub(r"[^a-z0-9.]+", "-", s.lower()).strip("-")

        task = slug(self.task)[:40]
        return (
            f"pm-{slug(self.experiment)}-{task}-{slug(self.model)}"
            f"-{slug(self.variant)}-t{self.trial_index}-{self.trial_id}"
        )


class TrialResult(BaseModel):
    schema_version: str = SCHEMA_VERSION
    # identity
    experiment: str | None = None
    trial_id: str | None = None
    job_name: str
    trial_dir: str | None = None
    backend: str
    suite: str | None = None
    task: str
    tier: str | None = None
    model: str | None = None
    variant: str | None = None
    variant_dir: str | None = None
    trial_index: int | None = None
    agent: str | None = None
    agent_version: str | None = None
    # outcome
    status: Status
    reward: float | None = None
    partial: float | None = None
    exception: str | None = None
    agent_timed_out: bool = False
    usage_missing: bool = Field(
        False,
        description=(
            "no usage captured (some CLIs only emit usage in a final result event, "
            "which a timed-out/killed agent never writes)"
        ),
    )
    # usage
    input_tokens: int | None = Field(None, description="non-cached input tokens")
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    prompt_tokens_total: int | None = Field(None, description="input + cache_read + cache_write")
    total_tokens: int | None = None
    cost_usd: float | None = None
    cost_source: str | None = Field(
        None,
        description=(
            "cursor_cli / claude_code / codex / noop = reported by the agent CLI (treated as billed); "
            "cursor_pricing / litellm = price-table estimate, not a bill"
        ),
    )
    cli_reported_cost_usd: float | None = None
    # behaviour
    n_turns: int | None = Field(None, description="model calls (distinct model_call_id / agent steps)")
    n_tool_calls: int | None = None
    tool_output_chars: int | None = Field(None, description="approx. model-visible chars of all tool results")
    tool_output_tokens_est: int | None = Field(None, description="tool_output_chars / 4 (approximation)")
    tool_calls_by_name: dict[str, int] | None = Field(
        None,
        description="tool calls per label; shell calls split by program (e.g. 'Bash:agent-browser'), MCP by tool",
    )
    tool_result_images: int | None = Field(None, description="images returned in tool results (e.g. screenshots)")
    n_shell_calls: int | None = None
    shell_output_chars: int | None = None
    shell_output_tokens_est: int | None = None
    # time
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_s: float | None = None
    env_setup_s: float | None = None
    agent_setup_s: float | None = None
    agent_s: float | None = None
    verifier_s: float | None = None
    # provenance / reproducibility
    primase_version: str | None = None
    primase_git_sha: str | None = None
    harbor_version: str | None = None
    pier_version: str | None = None
    config_hash: str | None = None
    variant_def: dict[str, Any] | None = Field(None, description="resolved variant definition used for this trial")
    variant_hash: str | None = None
    task_content_hash: str | None = None
    docker_image: str | None = None
    docker_image_digest: str | None = None
    network_mode: str | None = None
    trajectory_path: str | None = None
    agent_log_path: str | None = None
    verifier_path: str | None = None
    # hygiene
    secret_files_scrubbed: int = 0

    @property
    def passed(self) -> bool:
        return self.status == "passed"


CSV_FIELDS = list(TrialResult.model_fields)
