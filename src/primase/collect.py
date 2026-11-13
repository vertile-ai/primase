"""Collectors: backend job directory → TrialResult.

Harbor and Pier share the trial layout (Pier is a Harbor fork):

  <job>/<trial>/result.json            verifier rewards, agent_result tokens/cost, phase timestamps
  <job>/<trial>/agent/trajectory.json  ATIF (final_metrics, extra.usage_totals)
  <job>/<trial>/agent/cursor-cli.txt   cursor-agent --output-format=stream-json
  <job>/<trial>/agent/claude-code.txt  Claude Code stdout stream-json (type=result, total_cost_usd)
  <job>/<trial>/agent/sessions/**      Claude session jsonl or Codex rollout-*.jsonl
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .schema import Status, TrialResult

CURSOR_STREAM = "cursor-cli.txt"
CLAUDE_STREAM = "claude-code.txt"


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


def _empty_usage() -> dict[str, Any]:
    return {
        "usage": {"inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0, "cacheWriteTokens": 0},
        "has_usage": False,
        "reported_cost": None,
        "n_model_calls": 0,
        "tool_calls": 0,
        "tool_output_chars": 0,
        "shell_output_chars": 0,
        "shell_calls": 0,
        "result_events": 0,
        "is_error": None,
        "model": None,
        "cost_source": None,
    }


def _add_usage(out: dict[str, Any], inp: int, out_t: int, cr: int, cw: int) -> None:
    out["has_usage"] = True
    out["usage"]["inputTokens"] += inp
    out["usage"]["outputTokens"] += out_t
    out["usage"]["cacheReadTokens"] += cr
    out["usage"]["cacheWriteTokens"] += cw


# ---------------------------------------------------------------------------
# cursor-cli stream-json
# ---------------------------------------------------------------------------

_CLIENT_ONLY_KEYS = {
    "beforeFullFileContent",
    "afterFullFileContent",
    "relatedCursorRules",
    "relatedCursorRulePaths",
}


def _tool_output(name: str, result: Any) -> tuple[int, int]:
    """(model-visible chars ≈, shell output chars) for one completed cursor tool call.

    cursor-agent's stream carries client-side extras (full before/after file contents of edits,
    rule lists), so the raw JSON overstates what the model reads. Approximation: shell →
    interleavedOutput (or stdout+stderr), read → content, edit → diffString+message, others →
    JSON of the payload minus client-only keys.
    """
    if result is None:
        return 0, 0
    if isinstance(result, str):
        return len(result), (len(result) if name == "shellToolCall" else 0)
    payload = result
    if isinstance(result, dict) and len(result) >= 1:
        inner = next(
            (result[k] for k in ("success", "failure", "error", "rejected") if isinstance(result.get(k), dict)),
            None,
        )
        payload = inner if inner is not None else result
    if not isinstance(payload, dict):
        n = len(json.dumps(payload))
        return n, 0
    if name == "shellToolCall":
        out = payload.get("interleavedOutput")
        if out is None:
            out = (payload.get("stdout") or "") + (payload.get("stderr") or "")
        return len(out), len(out)
    if name == "readToolCall" and isinstance(payload.get("content"), str):
        return len(payload["content"]), 0
    if name == "editToolCall":
        return len(payload.get("diffString") or "") + len(payload.get("message") or ""), 0
    return len(json.dumps({k: v for k, v in payload.items() if k not in _CLIENT_ONLY_KEYS})), 0


def parse_cursor_stream(path: Path) -> dict[str, Any]:
    """Aggregate a cursor-agent stream-json log. Unknown/invalid lines are ignored."""
    out = _empty_usage()
    if not path.exists():
        return out
    model_calls: set[str] = set()
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        t = ev.get("type")
        if t == "system":
            out["model"] = ev.get("model") or out["model"]
        elif t == "assistant" and ev.get("model_call_id"):
            model_calls.add(ev["model_call_id"])
        elif t == "tool_call":
            if ev.get("model_call_id"):
                model_calls.add(ev["model_call_id"])
            if ev.get("subtype") == "completed":
                for name, call in (ev.get("tool_call") or {}).items():
                    if not isinstance(call, dict):
                        continue
                    out["tool_calls"] += 1
                    out["shell_calls"] += int(name == "shellToolCall")
                    vis, sh = _tool_output(name, call.get("result"))
                    out["tool_output_chars"] += vis
                    out["shell_output_chars"] += sh
        elif t == "result":
            out["result_events"] += 1
            out["is_error"] = bool(ev.get("is_error"))
            u = ev.get("usage")
            if isinstance(u, dict):
                _add_usage(
                    out,
                    int(u.get("inputTokens") or 0),
                    int(u.get("outputTokens") or 0),
                    int(u.get("cacheReadTokens") or 0),
                    int(u.get("cacheWriteTokens") or 0),
                )
                c = u.get("totalCost", u.get("cost"))
                if c is not None:
                    out["reported_cost"] = (out["reported_cost"] or 0.0) + float(c)
                    out["cost_source"] = "cursor_cli"
    out["n_model_calls"] = len(model_calls)
    return out


# ---------------------------------------------------------------------------
# Claude Code (stdout stream + session jsonl)
# ---------------------------------------------------------------------------


def _claude_usage_fields(u: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        int(u.get("input_tokens") or 0),
        int(u.get("output_tokens") or 0),
        int(u.get("cache_read_input_tokens") or 0),
        int(u.get("cache_creation_input_tokens") or 0),
    )


def parse_claude_stream(path: Path) -> dict[str, Any]:
    """Aggregate Claude Code's ``claude-code.txt`` stream-json (type=result, total_cost_usd)."""
    out = _empty_usage()
    if not path.exists():
        return out
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        t = ev.get("type")
        if t == "result":
            out["result_events"] += 1
            cost = ev.get("total_cost_usd", ev.get("total_cost"))
            if cost is not None:
                out["reported_cost"] = (out["reported_cost"] or 0.0) + float(cost)
                out["cost_source"] = "claude_code"
            u = ev.get("usage")
            if isinstance(u, dict):
                # result usage is the session total; replace any assistant running total
                out["usage"] = {
                    "inputTokens": 0,
                    "outputTokens": 0,
                    "cacheReadTokens": 0,
                    "cacheWriteTokens": 0,
                }
                out["has_usage"] = False
                _add_usage(out, *_claude_usage_fields(u))
        elif t == "assistant":
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else ev
            u = msg.get("usage") if isinstance(msg, dict) else None
            if isinstance(u, dict) and not out["result_events"]:
                # Claude accumulates; last assistant usage is a fallback if no result event
                tmp = _empty_usage()
                _add_usage(tmp, *_claude_usage_fields(u))
                out["usage"] = tmp["usage"]
                out["has_usage"] = True
            if ev.get("message", ev).get("model") if isinstance(ev.get("message"), dict) else ev.get("model"):
                model = (ev.get("message") or ev).get("model")
                if model:
                    out["model"] = model
    return out


def parse_claude_sessions(agent_dir: Path) -> dict[str, Any]:
    """Walk ``agent/sessions/projects/**/*.jsonl`` and take the last accumulated usage per session."""
    out = _empty_usage()
    root = agent_dir / "sessions" / "projects"
    if not root.exists():
        # also accept a flat sessions/ dir of uuid.jsonl files
        root = agent_dir / "sessions"
        if not root.exists():
            return out
    files = [p for p in root.rglob("*.jsonl") if p.is_file() and "rollout-" not in p.name]
    if not files:
        return out
    last_usage_by_msg: dict[str, dict[str, Any]] = {}
    tool_calls = 0
    tool_chars = 0
    for path in files:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else ev
            if not isinstance(msg, dict):
                continue
            mid = ev.get("uuid") or ev.get("id") or msg.get("id")
            usage = msg.get("usage") or ev.get("usage")
            if mid and isinstance(usage, dict):
                last_usage_by_msg[str(mid)] = usage
            typ = ev.get("type") or msg.get("type")
            if typ in {"tool_use", "tool_result"} or msg.get("role") == "tool":
                tool_calls += typ == "tool_use" or (
                    isinstance(msg.get("content"), list)
                    and any(isinstance(p, dict) and p.get("type") == "tool_use" for p in msg["content"])
                )
            content = msg.get("content")
            if typ == "tool_result" or (isinstance(content, str) and ev.get("type") == "user"):
                if isinstance(content, str):
                    tool_chars += len(content)
            if ev.get("cwd") is None and ev.get("type") == "assistant":
                model = msg.get("model")
                if model:
                    out["model"] = model
    if last_usage_by_msg:
        # Claude accumulates; the last usage on the longest chain is the session total.
        # Summing would double-count, so take the usage with the largest prompt+output.
        def _score(u: dict[str, Any]) -> int:
            i, o, cr, cw = _claude_usage_fields(u)
            return i + o + cr + cw

        best = max(last_usage_by_msg.values(), key=_score)
        _add_usage(out, *_claude_usage_fields(best))
    out["tool_calls"] = tool_calls
    out["tool_output_chars"] = tool_chars
    out["n_model_calls"] = len(last_usage_by_msg) or out["n_model_calls"]
    return out


# ---------------------------------------------------------------------------
# Codex rollout jsonl
# ---------------------------------------------------------------------------


def _codex_token_payload(ev: dict[str, Any]) -> dict[str, Any] | None:
    payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else ev
    if not isinstance(payload, dict):
        return None
    inner = payload.get("info") if isinstance(payload.get("info"), dict) else payload
    usage = None
    if isinstance(inner, dict):
        usage = inner.get("last_token_usage") or inner.get("token_usage")
        if usage is None and payload.get("type") in {"token_count", "event_msg"}:
            usage = inner if "input_tokens" in inner or "output_tokens" in inner else None
    if ev.get("type") == "token_count" and isinstance(ev.get("last_token_usage"), dict):
        usage = ev["last_token_usage"]
    return usage if isinstance(usage, dict) else None


def parse_codex_rollouts(agent_dir: Path) -> dict[str, Any]:
    """Parse ``agent/sessions/**/rollout-*.jsonl``; last_token_usage is cumulative."""
    out = _empty_usage()
    sessions = agent_dir / "sessions"
    if not sessions.exists():
        return out
    files = sorted(p for p in sessions.rglob("rollout-*.jsonl") if p.is_file())
    if not files:
        return out
    last: dict[str, Any] | None = None
    n_items = 0
    n_tools = 0
    tool_chars = 0
    for path in files:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            typ = ev.get("type")
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            ptype = payload.get("type") if isinstance(payload, dict) else None
            usage = _codex_token_payload(ev)
            if usage:
                last = usage
            if typ == "response_item" or ptype == "response_item":
                n_items += 1
            item = payload.get("item") if isinstance(payload, dict) else None
            if isinstance(item, dict) and item.get("type") == "function_call":
                n_tools += 1
            if isinstance(item, dict) and item.get("type") == "function_call_output":
                c = item.get("output")
                if isinstance(c, str):
                    tool_chars += len(c)
    if last:
        _add_usage(
            out,
            int(last.get("input_tokens") or 0),
            int(last.get("output_tokens") or 0),
            int(last.get("cached_input_tokens") or 0),
            int(last.get("cache_write_input_tokens") or 0),
        )
        out["cost_source"] = "codex" if last.get("cost") is not None else out["cost_source"]
        if last.get("cost") is not None:
            out["reported_cost"] = float(last["cost"])
    out["n_model_calls"] = n_items or out["n_model_calls"]
    out["tool_calls"] = n_tools
    out["tool_output_chars"] = tool_chars
    return out


# ---------------------------------------------------------------------------
# ATIF trajectory
# ---------------------------------------------------------------------------


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


def _prefer_usage(*cands: dict[str, Any]) -> dict[str, Any]:
    for c in cands:
        if c and c.get("has_usage"):
            return c
    for c in cands:
        if c and (c.get("reported_cost") is not None or c.get("tool_calls") or c.get("result_events")):
            return c
    return cands[0] if cands else _empty_usage()


def collect_trial(trial_dir: Path, backend: str, job_name: str | None = None) -> TrialResult:
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
    cursor = parse_cursor_stream(agent_dir / CURSOR_STREAM)
    claude = parse_claude_stream(agent_dir / CLAUDE_STREAM)
    if not claude["has_usage"]:
        claude_sess = parse_claude_sessions(agent_dir)
        claude = _prefer_usage(claude, claude_sess)
    codex = parse_codex_rollouts(agent_dir)
    stream = _prefer_usage(cursor, claude, codex)

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
    model = model or stream.get("model")
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
        model=model,
        agent=ai.get("name"),
        agent_version=ai.get("version") or tstats.get("agent_version"),
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


def collect_job(job_dir: Path, backend: str) -> list[TrialResult]:
    job_dir = Path(job_dir)
    if not job_dir.is_dir():
        return []
    trials = sorted(p for p in job_dir.iterdir() if p.is_dir() and "__" in p.name)
    return [collect_trial(t, backend, job_dir.name) for t in trials]


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
