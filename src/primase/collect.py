"""Collectors: backend job directory → TrialResult.

Harbor and Pier share the trial layout (Pier is a Harbor fork):

  <job>/<trial>/result.json            verifier rewards, agent_result tokens/cost, phase timestamps
  <job>/<trial>/agent/trajectory.json  ATIF (final_metrics, extra.usage_totals)
  <job>/<trial>/agent/<plugin-files>   parsed via the agent plugin registry
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .agents import claude_code as _claude
from .agents import codex as _codex
from .agents import collect_agent_metrics
from .agents import cursor_cli as _cursor
from .schema import Status, TrialResult

# Backward-compatible names
CURSOR_STREAM = "cursor-cli.txt"
CLAUDE_STREAM = "claude-code.txt"
_tool_output = _cursor.tool_output


def _read_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _ts(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _span(d: dict[str, Any] | None) -> float | None:
    if not isinstance(d, dict):
        return None
    a, b = _ts(d.get("started_at")), _ts(d.get("finished_at"))
    if a and b:
        if (a.tzinfo is None) != (b.tzinfo is None):
            a, b = a.replace(tzinfo=None), b.replace(tzinfo=None)
        return round((b - a).total_seconds(), 3)
    return None


def _trajectory_stats(traj: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(traj, dict):
        return {}
    steps = traj.get("steps") or []
    agent_steps = [s for s in steps if s.get("source") == "agent"]
    n_tools = sum(len(s.get("tool_calls") or []) for s in agent_steps)
    chars = 0
    for s in agent_steps:
        for r in (s.get("observation") or {}).get("results") or []:
            c = r.get("content")
            if c is not None:
                chars += len(c if isinstance(c, str) else json.dumps(c))
    fm = traj.get("final_metrics") or {}
    return {
        "n_turns": len(agent_steps),
        "n_tool_calls": n_tools,
        "tool_output_chars": chars,
        "final_metrics": fm,
        "agent_version": (traj.get("agent") or {}).get("version"),
    }


def _metrics_to_legacy_dict(m: Any) -> dict[str, Any]:
    """Adapt AgentMetrics to the dict shape older helpers / tests expect."""
    return {
        "usage": {
            "inputTokens": m.input_tokens,
            "outputTokens": m.output_tokens,
            "cacheReadTokens": m.cache_read_tokens,
            "cacheWriteTokens": m.cache_write_tokens,
        },
        "has_usage": m.has_usage,
        "reported_cost": m.reported_cost_usd,
        "n_model_calls": m.n_turns or 0,
        "tool_calls": m.n_tool_calls or 0,
        "tool_output_chars": m.tool_output_chars or 0,
        "shell_output_chars": m.shell_output_chars or 0,
        "shell_calls": m.n_shell_calls or 0,
        "result_events": m.result_events,
        "is_error": m.is_error,
        "model": m.model,
        "cost_source": m.cost_source,
    }


def parse_cursor_stream(path: Path) -> dict[str, Any]:
    return _metrics_to_legacy_dict(_cursor.parse_cursor_stream(path))


def parse_claude_stream(path: Path) -> dict[str, Any]:
    return _metrics_to_legacy_dict(_claude.parse_claude_stream(path))


def parse_claude_sessions(agent_dir: Path) -> dict[str, Any]:
    return _metrics_to_legacy_dict(_claude.parse_claude_sessions(agent_dir))


def parse_codex_rollouts(agent_dir: Path) -> dict[str, Any]:
    return _metrics_to_legacy_dict(_codex.parse_codex_rollouts(agent_dir))


def collect_trial(
    trial_dir: Path,
    backend: str,
    job_name: str | None = None,
    preferred_agent: str | None = None,
) -> TrialResult:
    trial_dir = Path(trial_dir)
    res = _read_json(trial_dir / "result.json") or {}
    job_name = job_name or trial_dir.parent.name
    folder = trial_dir.name.rsplit("__", 1)[0]
    task = folder or str(res.get("task_name", "")).split("/")[-1]
    if not res:
        return TrialResult(job_name=job_name, trial_dir=str(trial_dir), backend=backend, task=task, status="missing")

    ar = res.get("agent_result") or {}
    ai = res.get("agent_info") or {}
    vr = res.get("verifier_result") or {}
    rewards = vr.get("rewards") or _read_json(trial_dir / "verifier" / "reward.json") or {}
    if not rewards and (trial_dir / "verifier" / "reward.txt").exists():
        try:
            rewards = {"reward": float((trial_dir / "verifier" / "reward.txt").read_text().strip())}
        except ValueError:
            rewards = {}
    reward = rewards.get("reward")
    exc = res.get("exception_info")
    exc_s = None
    if exc:
        exc_s = exc.get("exception_type") if isinstance(exc, dict) else str(exc)
        if isinstance(exc, dict) and exc.get("exception_message"):
            exc_s = f"{exc_s}: {str(exc['exception_message'])[:300]}"
    status: Status
    if reward is None:
        status = "error"
    else:
        status = "passed" if float(reward) >= 1.0 else "failed"

    agent_dir = trial_dir / "agent"
    preferred = preferred_agent or ai.get("name")
    metrics = collect_agent_metrics(agent_dir, preferred=preferred)
    stream = _metrics_to_legacy_dict(metrics)

    tstats = _trajectory_stats(_read_json(agent_dir / "trajectory.json"))
    fm = tstats.get("final_metrics") or {}
    extra = fm.get("extra") or {}

    if stream["has_usage"]:
        u = stream["usage"]
    elif isinstance(extra.get("usage_totals"), dict):
        u = extra["usage_totals"]
    else:
        u = None
    inp: int | None = None
    out_t: int | None = None
    cr: int | None = None
    cw: int | None = None
    if u:
        inp = int(u.get("inputTokens", 0))
        out_t = int(u.get("outputTokens", 0))
        cr = int(u.get("cacheReadTokens", 0))
        cw = int(u.get("cacheWriteTokens", 0))
        prompt_total: int | None = inp + cr + cw
    else:
        raw = ar.get("n_input_tokens") or fm.get("total_prompt_tokens")
        prompt_total = int(raw) if raw is not None else None
    if out_t is None:
        out_t = ar.get("n_output_tokens") or fm.get("total_completion_tokens")
    if cr is None:
        cr = ar.get("n_cache_tokens") or fm.get("total_cached_tokens")

    cost = ar.get("cost_usd")
    if cost is None:
        cost = fm.get("total_cost_usd")
    cost_source = extra.get("cost_source") or stream.get("cost_source")
    if stream.get("reported_cost") is not None:
        cost = cost if cost is not None else stream["reported_cost"]
        cost_source = stream.get("cost_source") or cost_source

    sh_chars = n_shell = None
    if stream.get("result_events") or stream.get("tool_calls") or stream.get("has_usage"):
        n_turns = stream["n_model_calls"] or tstats.get("n_turns")
        n_tools, chars = stream["tool_calls"], stream["tool_output_chars"]
        sh_chars, n_shell = stream["shell_output_chars"], stream["shell_calls"]
    else:
        n_turns, n_tools, chars = tstats.get("n_turns"), tstats.get("n_tool_calls"), tstats.get("tool_output_chars")

    model_info = ai.get("model_info") or {}
    model = model_info.get("name") if isinstance(model_info, dict) else None
    model = model or ((res.get("config") or {}).get("agent") or {}).get("model_name")
    model = model or stream.get("model") or metrics.model
    if model and "/" in str(model):
        model = str(model).split("/", 1)[1]

    timed_out = bool(exc_s and "Timeout" in exc_s.split(":", 1)[0])
    return TrialResult(
        job_name=job_name,
        trial_dir=str(trial_dir),
        backend=backend,
        task=task,
        agent_timed_out=timed_out,
        usage_missing=u is None and prompt_total is None,
        usage_partial=bool(metrics.extra.get("usage_partial")),
        model=model,
        agent=ai.get("name"),
        agent_version=ai.get("version") or tstats.get("agent_version") or metrics.agent_version,
        status=status,
        reward=reward,
        partial=rewards.get("partial"),
        exception=exc_s,
        input_tokens=inp,
        output_tokens=out_t,
        cache_read_tokens=cr,
        cache_write_tokens=cw,
        prompt_tokens_total=prompt_total,
        total_tokens=(prompt_total + out_t) if (prompt_total is not None and out_t is not None) else None,
        cost_usd=cost,
        cost_source=cost_source,
        cli_reported_cost_usd=stream.get("reported_cost"),
        n_turns=n_turns,
        n_tool_calls=n_tools,
        tool_output_chars=chars,
        tool_output_tokens_est=(chars // 4) if chars is not None else None,
        tool_calls_by_name=metrics.extra.get("tool_calls_by_name") or None,
        tool_result_images=metrics.extra.get("tool_result_images"),
        n_shell_calls=n_shell,
        shell_output_chars=sh_chars,
        shell_output_tokens_est=(sh_chars // 4) if sh_chars is not None else None,
        started_at=_ts(res.get("started_at")),
        finished_at=_ts(res.get("finished_at")),
        duration_s=_span(res),
        env_setup_s=_span(res.get("environment_setup")),
        agent_setup_s=_span(res.get("agent_setup")),
        agent_s=_span(res.get("agent_execution")),
        verifier_s=_span(res.get("verifier")),
    )


def collect_job(job_dir: Path, backend: str, preferred_agent: str | None = None) -> list[TrialResult]:
    job_dir = Path(job_dir)
    if not job_dir.is_dir():
        return []
    trials = sorted(p for p in job_dir.iterdir() if p.is_dir() and "__" in p.name)
    return [collect_trial(t, backend, job_dir.name, preferred_agent=preferred_agent) for t in trials]


def job_complete(job_dir: Path) -> bool:
    """A job counts as done when every trial has a result.json with finished_at (resume skips it)."""
    job_dir = Path(job_dir)
    trials = [p for p in job_dir.iterdir() if p.is_dir() and "__" in p.name] if job_dir.is_dir() else []
    if not trials:
        return False
    for t in trials:
        r = _read_json(t / "result.json")
        if not r or not r.get("finished_at"):
            return False
    return True
