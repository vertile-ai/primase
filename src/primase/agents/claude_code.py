"""Claude Code plugin: stdout stream-json + session jsonl collectors."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

from .base import AgentMetrics, AgentPlugin, LocalCommand, LocalInvocation, generic_local_command, mcp_config

STREAM = "claude-code.txt"


def _claude_usage_fields(u: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        int(u.get("input_tokens") or 0),
        int(u.get("output_tokens") or 0),
        int(u.get("cache_read_input_tokens") or 0),
        int(u.get("cache_creation_input_tokens") or 0),
    )


_SHELL_TOOLS = {"Bash", "bash", "shell", "run_shell_command"}


def tool_label(name: str, tool_input: Any) -> str:
    """Stable per-tool label. Shell calls are split by program (``Bash:agent-browser``) so a report can tell
    which CLI the agent drove; MCP tools keep their ``mcp__server__tool`` name."""
    if name in _SHELL_TOOLS and isinstance(tool_input, dict):
        cmd = str(tool_input.get("command") or "").strip()
        prog = first_program(cmd)
        return f"{name}:{prog}" if prog else name
    return name


_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SEPARATORS = {"&&", "||", ";", "|"}
_WRAPPERS = {"time", "sudo", "env", "exec", "npx", "bunx", "command", "nohup"}


def first_program(cmd: str) -> str | None:
    """First executable in a shell command, skipping ``VAR=val`` prefixes, ``cd dir &&``, wrappers
    (``npx``, ``env``, ``time`` …) and their flags, and directory paths."""
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        tokens = cmd.split()
    in_cd = False
    for tok in tokens:
        if in_cd:
            in_cd = tok not in _SEPARATORS
            continue
        if tok in _SEPARATORS or _ASSIGN.match(tok) or tok in _WRAPPERS or tok.startswith("-"):
            continue
        if tok in {"cd", "pushd"}:
            in_cd = True
            continue
        return tok.rsplit("/", 1)[-1]
    return None


def _content_chars(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        n = 0
        for part in content:
            if isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    n += len(part["text"])
                elif part.get("type") == "image":
                    n += 0  # images are billed as tokens but have no chars; tracked via n_images
            elif isinstance(part, str):
                n += len(part)
        return n
    return 0


def parse_claude_stream(path: Path) -> AgentMetrics:
    """Aggregate Claude Code's ``claude-code.txt`` stream-json (type=result, total_cost_usd).

    Usage = sum over ``result`` events. Without any result event carrying usage (agent killed /
    timed out) it falls back to the sum over distinct assistant message ids (each id counted once,
    last event wins) and sets ``extra['usage_partial'] = True`` — a lower bound.

    Besides usage this counts model turns (distinct assistant message ids), tool calls (with a
    per-tool label in ``extra['tool_calls_by_name']``), and model-visible tool-result chars.
    """
    out = AgentMetrics()
    if not path.exists():
        return out
    result_usage = [0, 0, 0, 0]
    has_result_usage = False
    usage_by_msg: dict[str, dict[str, Any]] = {}
    msg_ids: set[str] = set()
    by_name: dict[str, int] = {}
    shell_ids: set[str] = set()
    tool_calls = tool_chars = shell_chars = n_images = 0
    for lineno, line in enumerate(path.read_text(errors="replace").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        t = ev.get("type")
        if t == "result":
            out.result_events += 1
            cost = ev.get("total_cost_usd", ev.get("total_cost"))
            if cost is not None:
                out.reported_cost_usd = (out.reported_cost_usd or 0.0) + float(cost)
                out.cost_source = "claude_code"
            u = ev.get("usage")
            if isinstance(u, dict):
                has_result_usage = True
                for k, v in enumerate(_claude_usage_fields(u)):
                    result_usage[k] += v
        elif t == "assistant":
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else ev
            u = msg.get("usage") if isinstance(msg, dict) else None
            if isinstance(u, dict):
                usage_by_msg[str(msg.get("id") or f"_line{lineno}")] = u
            if isinstance(msg, dict) and msg.get("model"):
                out.model = msg["model"]
            elif isinstance(ev, dict) and ev.get("model"):
                out.model = ev["model"]
            if isinstance(msg, dict):
                if msg.get("id"):
                    msg_ids.add(str(msg["id"]))
                for part in msg.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "tool_use":
                        tool_calls += 1
                        label = tool_label(str(part.get("name") or "?"), part.get("input"))
                        by_name[label] = by_name.get(label, 0) + 1
                        if str(part.get("name")) in _SHELL_TOOLS and part.get("id"):
                            shell_ids.add(str(part["id"]))
        elif t == "user":
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else ev
            for part in (msg.get("content") if isinstance(msg, dict) else None) or []:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    c = part.get("content")
                    n = _content_chars(c)
                    tool_chars += n
                    if isinstance(c, list):
                        n_images += sum(1 for x in c if isinstance(x, dict) and x.get("type") == "image")
                    if str(part.get("tool_use_id")) in shell_ids:
                        shell_chars += n
        elif t == "system" and ev.get("subtype") == "init" and ev.get("claude_code_version"):
            out.agent_version = str(ev["claude_code_version"])
    if has_result_usage:
        out.has_usage = True
        out.input_tokens, out.output_tokens, out.cache_read_tokens, out.cache_write_tokens = result_usage
    elif usage_by_msg:
        totals = [0, 0, 0, 0]
        for u in usage_by_msg.values():
            for k, v in enumerate(_claude_usage_fields(u)):
                totals[k] += v
        out.has_usage = True
        out.input_tokens, out.output_tokens, out.cache_read_tokens, out.cache_write_tokens = totals
        out.extra["usage_partial"] = True
    if tool_calls or msg_ids:
        out.n_turns = len(msg_ids) or out.n_turns
        out.n_tool_calls = tool_calls
        out.tool_output_chars = tool_chars
        out.n_shell_calls = len(shell_ids)
        out.shell_output_chars = shell_chars
        out.extra["tool_calls_by_name"] = dict(sorted(by_name.items()))
        if n_images:
            out.extra["tool_result_images"] = n_images
    return out


def parse_claude_sessions(agent_dir: Path) -> AgentMetrics:
    """Walk ``agent/sessions/projects/**/*.jsonl`` and take the last accumulated usage per session."""
    out = AgentMetrics()
    root = agent_dir / "sessions" / "projects"
    if not root.exists():
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
                    out.model = model
    if last_usage_by_msg:

        def _score(u: dict[str, Any]) -> int:
            i, o, cr, cw = _claude_usage_fields(u)
            return i + o + cr + cw

        best = max(last_usage_by_msg.values(), key=_score)
        i, o, cr, cw = _claude_usage_fields(best)
        out.has_usage = True
        out.input_tokens, out.output_tokens = i, o
        out.cache_read_tokens, out.cache_write_tokens = cr, cw
    out.n_tool_calls = tool_calls
    out.tool_output_chars = tool_chars
    out.n_turns = len(last_usage_by_msg) or out.n_turns
    return out


def _prefer(a: AgentMetrics, b: AgentMetrics) -> AgentMetrics:
    if a.has_usage:
        return a
    if b.has_usage:
        return b
    if a.reported_cost_usd is not None or a.n_tool_calls or a.result_events:
        return a
    if b.reported_cost_usd is not None or b.n_tool_calls or b.result_events:
        return b
    return a


class ClaudeCodePlugin(AgentPlugin):
    name = "claude-code"
    billed_cost_sources = frozenset({"claude_code"})

    def collect(self, agent_dir: Path) -> AgentMetrics:
        agent_dir = Path(agent_dir)
        stream = parse_claude_stream(agent_dir / STREAM)
        if stream.has_usage:
            return stream
        return _prefer(stream, parse_claude_sessions(agent_dir))

    def local_command(self, inv: LocalInvocation) -> LocalCommand | None:
        """``claude -p`` headless, stream-json to ``claude-code.txt`` (same file Harbor's agent writes).

        Every arm gets the same host isolation: ``<agent_dir>/mcp.json`` is always written (empty
        ``mcpServers`` when the variant has none) and passed with ``--strict-mcp-config``, so user-level
        MCP servers never leak in; ``--setting-sources`` excludes user settings/hooks/plugins by default.

        kwargs: ``permission_mode`` (default ``bypassPermissions`` — the trial runs unattended; use a
        throwaway workspace), ``setting_sources`` (default ``project``), ``allowed_tools`` /
        ``disallowed_tools`` (list), ``max_turns`` (int), ``extra_args`` (list), ``binary`` (default ``claude``).
        """
        generic = generic_local_command(inv, default_stdout=STREAM)
        if generic is not None:
            return generic
        kw = inv.kwargs
        argv = [str(kw.get("binary") or "claude"), "-p", inv.instruction, "--output-format", "stream-json", "--verbose"]
        if inv.model:
            argv += ["--model", inv.model]
        argv += ["--permission-mode", str(kw.get("permission_mode") or "bypassPermissions")]
        mcp_path = inv.agent_dir / "mcp.json"
        argv += ["--mcp-config", str(mcp_path), "--strict-mcp-config"]
        argv += ["--setting-sources", str(kw.get("setting_sources") or "project")]
        for flag, key in (("--allowedTools", "allowed_tools"), ("--disallowedTools", "disallowed_tools")):
            vals = kw.get(key)
            if vals:
                argv += [flag, ",".join(vals) if isinstance(vals, list) else str(vals)]
        if kw.get("max_turns"):
            argv += ["--max-turns", str(kw["max_turns"])]
        argv += [str(a) for a in kw.get("extra_args") or []]
        files = {str(mcp_path): json.dumps(mcp_config(inv.mcp_servers), indent=2)}
        return LocalCommand(argv=argv, stdout_file=STREAM, files=files)
