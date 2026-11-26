"""Tool-as-variant: instruction/MCP/file overlays, the local (host) backend, tool-call metrics, verdicts."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from primase.agents.claude_code import first_program, parse_claude_stream
from primase.backends import get_backend
from primase.config import load_config
from primase.orchestrator import Orchestrator, plan
from primase.report import markdown, row_from_csv, verdicts, write_csv
from primase.schema import TrialResult
from primase.secrets import SecretStore
from primase.toml_edit import append_array_tables, iter_tables, set_table_key

# A fake "agent CLI": emits a Claude-Code-shaped stream-json and writes answer.txt in its cwd.
FAKE_AGENT = r"""
import json, os, sys
answer, tool, tokens = sys.argv[1], sys.argv[2], int(sys.argv[3])
open("answer.txt", "w").write(answer)
def out(o): print(json.dumps(o))
out({"type": "system", "subtype": "init", "claude_code_version": "9.9.9"})
out({"type": "assistant", "message": {"id": "m1", "model": "fake", "content": [
    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": tool + " open http://x"}}]}})
out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok " * 10}]}})
out({"type": "assistant", "message": {"id": "m2", "model": "fake", "content": [
    {"type": "tool_use", "id": "t2", "name": "mcp__pw__browser_snapshot", "input": {}}]}})
out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2",
    "content": [{"type": "text", "text": "tree"}]}]}})
out({"type": "result", "total_cost_usd": 0, "usage": {"input_tokens": tokens, "output_tokens": 10,
    "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}})
"""


def _task(root: Path, name: str, expected: str) -> None:
    d = root / "work" / "variants" / "web" / "shared" / name
    (d / "tests").mkdir(parents=True)
    (d / "environment" / "workspace").mkdir(parents=True)
    (d / "environment" / "workspace" / "seed.txt").write_text(name)
    (d / "environment" / "site").mkdir()
    (d / "environment" / "site" / "secret-answer.txt").write_text(expected)  # must NOT reach the workspace
    (d / "task.toml").write_text('version = "1.0"\n[agent]\ntimeout_sec = 30.0\n[verifier]\ntimeout_sec = 30.0\n')
    (d / "instruction.md").write_text("Open http://127.0.0.1:{{PRIMASE_PORT}}/ and write the code to answer.txt.\n")
    (d / "tests" / "test.sh").write_text(
        "set -e\n"
        '[ ! -e site/secret-answer.txt ] || { echo 0 > "$PRIMASE_VERIFIER_DIR/reward.txt"; exit 0; }\n'
        f'if [ "$(cat answer.txt)" = "{expected}" ]; then echo 1 > "$PRIMASE_VERIFIER_DIR/reward.txt"; '
        'else echo 0 > "$PRIMASE_VERIFIER_DIR/reward.txt"; fi\n'
    )


@pytest.fixture
def local_bench(tmp_path: Path) -> Path:
    root = tmp_path / "bench"
    (root / "experiments").mkdir(parents=True)
    (root / "prompts").mkdir()
    (root / "prompts" / "tool-a.md").write_text("Use tool-a for all browsing.")
    (root / "skills" / "tool-a").mkdir(parents=True)
    (root / "skills" / "tool-a" / "SKILL.md").write_text("# tool-a skill")
    (root / "fake_agent.py").write_text(FAKE_AGENT)
    (root / "suites.json").write_text(
        json.dumps({"suites": [{"id": "web", "runner": "local", "tasks": {"t1": {}, "t2": {}}}]})
    )
    _task(root, "t1", "ABC")
    _task(root, "t2", "XYZ")
    return root


def _cfg(root: Path, variants: dict, **over):
    data = {
        "name": "tools",
        "root": "..",
        "agent": {"name": "claude-code"},
        "models": [{"id": "fake-model", "provider_prefix": "anthropic"}],
        "suites": [{"id": "web"}],
        "variants": variants,
        "baseline": next(iter(variants)),
        "budget": {"stop_on_billed_cost": False},
    }
    data.update(over)
    p = root / "experiments" / "e.yaml"
    p.write_text(yaml.safe_dump(data))
    return load_config(p)


def _variant(root: Path, tool: str, tokens: int, wrong: bool = False, **extra) -> dict:
    agent_py = str(root / "fake_agent.py")
    cmd = (
        ": {mcp_config}; env | grep '^PRIMASE_' | cut -d= -f1 | sort > agent-env.txt; "
        'a=$(case "$(cat seed.txt)" in t1) echo ABC;; *) echo XYZ;; esac); '
        f'{"a=WRONG; " if wrong else ""}"{sys.executable}" "{agent_py}" "$a" {tool} {tokens}'
    )
    v = {"dir": "shared", "agent": {"kwargs": {"local_command": ["bash", "-c", cmd]}}}
    v.update(extra)
    return v


def test_variant_overlay_fields_validate(local_bench):
    with pytest.raises(Exception, match="stdio transport needs 'command'"):
        _cfg(local_bench, {"a": {"dir": "shared", "mcp_servers": [{"name": "pw"}]}})
    cfg = _cfg(
        local_bench,
        {
            "a": {
                "dir": "shared",
                "instruction_prepend": "@prompts/tool-a.md",
                "overlay_files": {"environment/skills/tool-a": "skills/tool-a"},
                "mcp_servers": [{"name": "pw", "command": "npx", "args": ["@playwright/mcp@latest"]}],
            }
        },
    )
    assert cfg.variants["a"].has_task_overlay()


def test_harbor_overlay_renders_instruction_mcp_and_files(local_bench):
    cfg = _cfg(
        local_bench,
        {
            "a": {
                "dir": "shared",
                "instruction_prepend": "@prompts/tool-a.md",
                "instruction_append": "Write answer.txt.",
                "overlay_files": {"environment/skills/tool-a": "skills/tool-a"},
                "mcp_servers": [{"name": "pw", "command": "npx", "args": ["-y", "@playwright/mcp@latest"]}],
                "task_toml": {"agent": {"timeout_sec": 99}},
            }
        },
    )
    p = plan(cfg)[0][0]
    spec = p.spec.model_copy(update={"backend": "harbor"})
    src_task = p.ctx.task_dir
    b = get_backend("harbor", p.ctx.root)
    b.prepare_run(spec, p.ctx)
    od = p.ctx.task_dir
    assert od != src_task and ".overlays" in str(od)
    instr = (od / "instruction.md").read_text()
    assert instr.startswith("Use tool-a for all browsing.") and instr.rstrip().endswith("Write answer.txt.")
    toml = tomllib.loads((od / "task.toml").read_text())
    assert toml["agent"]["timeout_sec"] == 99
    assert toml["agent"]["network_mode"] == "public"
    assert toml["environment"]["mcp_servers"] == [
        {"name": "pw", "transport": "stdio", "command": "npx", "args": ["-y", "@playwright/mcp@latest"]}
    ]
    assert (od / "environment" / "skills" / "tool-a" / "SKILL.md").read_text() == "# tool-a skill"
    # source task untouched (symlinked parent dir was materialized, not written through)
    assert not (src_task / "environment" / "skills").exists()
    assert (src_task / "instruction.md").read_text().startswith("Open http")


def test_array_tables_are_table_boundaries():
    text = '[agent]\ntimeout_sec = 1\n\n[[environment.mcp_servers]]\nname = "a"\n'
    names = [n for n, _, _ in iter_tables(text)]
    assert names == ["agent", "[[environment.mcp_servers]]"]
    patched = set_table_key(text, "agent", "network_mode", "public")
    parsed = tomllib.loads(patched)
    assert parsed["agent"]["network_mode"] == "public"
    assert parsed["environment"]["mcp_servers"] == [{"name": "a"}]
    out, notes = append_array_tables("", "environment.mcp_servers", [{"name": "b", "args": ["x"], "url": None}])
    assert tomllib.loads(out)["environment"]["mcp_servers"] == [{"name": "b", "args": ["x"]}]
    assert notes


def test_first_program_labels():
    assert first_program("cd /tmp && FOO=1 ego-browser nodejs -e 'x'") == "ego-browser"
    assert first_program("npx -y agent-browser snapshot -i") == "agent-browser"
    assert first_program("/usr/bin/python3 x.py") == "python3"
    assert first_program("") is None


def test_claude_stream_tool_metrics(tmp_path):
    lines = [
        {"type": "system", "subtype": "init", "claude_code_version": "2.0.1"},
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "content": [
                    {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "agent-browser open u"}}
                ],
            },
        },
        {"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "same turn"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a", "content": "12345"}]}},
        {
            "type": "assistant",
            "message": {"id": "m2", "content": [{"type": "tool_use", "id": "b", "name": "mcp__pw__browser_click"}]},
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "b",
                        "content": [{"type": "text", "text": "abc"}, {"type": "image", "source": {}}],
                    }
                ]
            },
        },
        {"type": "result", "total_cost_usd": 0.5, "usage": {"input_tokens": 7, "output_tokens": 3}},
    ]
    p = tmp_path / "claude-code.txt"
    p.write_text("\n".join(json.dumps(x) for x in lines))
    m = parse_claude_stream(p)
    assert m.has_usage and m.input_tokens == 7
    assert m.n_turns == 2 and m.n_tool_calls == 2
    assert m.tool_output_chars == 8 and m.shell_output_chars == 5 and m.n_shell_calls == 1
    assert m.extra["tool_calls_by_name"] == {"Bash:agent-browser": 1, "mcp__pw__browser_click": 1}
    assert m.extra["tool_result_images"] == 1
    assert m.agent_version == "2.0.1"


def test_local_backend_end_to_end(local_bench):
    cfg = _cfg(
        local_bench,
        {
            "tool-b": _variant(local_bench, "tool-b", 1000),
            "tool-a": _variant(
                local_bench,
                "tool-a",
                600,
                instruction_prepend="@prompts/tool-a.md",
                mcp_servers=[{"name": "pw", "command": "npx", "args": ["@playwright/mcp@latest"]}],
            ),
            "broken": _variant(local_bench, "tool-c", 100, wrong=True),
        },
    )
    trials, _ = plan(cfg)
    assert {t.spec.backend for t in trials} == {"local"}
    orch = Orchestrator(cfg, SecretStore.load([], None, environ={}))
    for _p, v in orch.validate(trials):
        assert v.ok, v.messages
    rows, abort = orch.run(trials)
    assert abort is None
    by = {(r.task, r.variant): r for r in rows}
    assert len(by) == 6
    a1 = by[("t1", "tool-a")]
    assert a1.status == "passed", a1.exception
    assert a1.input_tokens == 600 and a1.total_tokens == 610
    assert a1.tool_calls_by_name == {"Bash:tool-a": 1, "mcp__pw__browser_snapshot": 1}
    assert a1.n_turns == 2 and a1.agent_version == "9.9.9"
    assert by[("t2", "broken")].status == "failed"
    trial_dir = Path(a1.trial_dir or "")
    ws = Path(json.loads((trial_dir / "result.json").read_text())["workspace"])
    assert ws.name.startswith("primase-ws-") and trial_dir not in ws.parents
    assert (ws / "seed.txt").exists()
    assert not (ws / "site").exists()  # only environment/workspace is seeded
    # the agent is not handed paths to the task / trial / verifier dirs
    assert (ws / "agent-env.txt").read_text().split() == ["PRIMASE_PORT", "PRIMASE_WORKSPACE"]
    instr = (trial_dir / "agent" / "instruction.md").read_text()
    assert instr.startswith("Use tool-a") and "{{PRIMASE_PORT}}" not in instr
    mcp = json.loads((trial_dir / "agent" / "mcp.json").read_text())
    assert mcp == {"mcpServers": {"pw": {"command": "npx", "args": ["@playwright/mcp@latest"]}}}
    b1 = Path(by[("t1", "tool-b")].trial_dir or "")
    assert json.loads((b1 / "agent" / "mcp.json").read_text()) == {"mcpServers": {}}
    # resume: second run skips completed jobs and re-collects identical rows
    rows2, _ = orch.run(trials)
    assert sorted(r.trial_id or "" for r in rows2) == sorted(r.trial_id or "" for r in rows)

    heads = {h["variant"]: h for h in verdicts(rows, baseline="tool-b")}
    assert heads["tool-a"]["verdict"] == "inconclusive (n=2 paired tasks, need 6)"
    assert heads["tool-a"]["total_tokens_median"] == pytest.approx(-39.6, abs=0.1)
    assert heads["broken"]["verdict"] == "REGRESSION"
    md = markdown(rows, "t", baseline="tool-b")
    assert md.index("## Verdict") < md.index("## Pass rate")
    assert "regressed — token delta not credited" in md
    assert "`Bash:tool-a` 2" in md


def test_local_backend_validate_reports_missing_cli(local_bench):
    cfg = _cfg(local_bench, {"a": {"dir": "shared", "agent": {"kwargs": {"binary": "definitely-not-a-cli-xyz"}}}})
    p = plan(cfg)[0][0]
    v = get_backend("local", p.ctx.root).validate(p.spec, p.ctx)
    assert not v.ok and any("not found on PATH" in m for m in v.messages)


def test_local_backend_validate_reports_missing_tool_command(local_bench):
    cfg = _cfg(local_bench, {"a": {"dir": "shared", "require_commands": ["sh", "definitely-not-a-tool-xyz"]}})
    p = plan(cfg)[0][0]
    assert p.ctx.require_commands == ["sh", "definitely-not-a-tool-xyz"]
    v = get_backend("local", p.ctx.root).validate(p.spec, p.ctx)
    msgs = [m for m in v.messages if "requires command" in m]
    assert not v.ok and msgs == ["variant requires command not on PATH: definitely-not-a-tool-xyz"]


def test_csv_roundtrip_with_tool_dict(tmp_path):
    r = TrialResult(job_name="j", backend="local", task="t", status="passed", tool_calls_by_name={"Bash:x": 2})
    path = write_csv([r], tmp_path / "t.csv")
    import csv

    with path.open() as f:
        rec = next(csv.DictReader(f))
    back = row_from_csv(rec)
    assert back.tool_calls_by_name == {"Bash:x": 2} and back.status == "passed"


def test_variant_agent_kwargs_keep_experiment_agent(local_bench):
    """Regression: `agent: {kwargs: ...}` on a variant used to silently switch to cursor-cli."""
    cfg = _cfg(local_bench, {"a": {"dir": "shared", "agent": {"kwargs": {"x": "1"}}}, "b": {"dir": "shared"}})
    assert cfg.variants["a"].resolved_agent(cfg.agent).name == "claude-code"
    assert cfg.variants["a"].resolved_agent(cfg.agent).kwargs == {"x": "1"}
    cfg2 = _cfg(local_bench, {"a": {"dir": "shared", "agent": {"name": "codex"}}})
    assert cfg2.variants["a"].resolved_agent(cfg2.agent).name == "codex"


def test_render_instruction_placeholders():
    from primase.local_runner import render_instruction

    out = render_instruction("go to {{env:SITE}}/x on port {{PRIMASE_PORT}}", 1234, {"SITE": "http://h"})
    assert out == "go to http://h/x on port 1234"
    with pytest.raises(KeyError, match="MISSING"):
        render_instruction("{{env:MISSING}}", 1, {})


def test_agent_plugins_own_mcp_delivery_and_host_isolation(tmp_path):
    """S7/B1: claude-code always gets a strict (possibly empty) MCP config and project-only settings;
    cursor-cli gets .cursor/mcp.json only with servers; local_command never touches disk."""
    from primase.agents import LocalInvocation, get_agent

    ws, ad = tmp_path / "ws", tmp_path / "agent"
    srv = [{"name": "pw", "transport": "stdio", "command": "npx", "args": ["x"]}]
    cc = get_agent("claude-code").local_command(LocalInvocation("go", "m", ws, ad))
    assert cc is not None
    i = cc.argv.index("--mcp-config")
    assert cc.argv[i + 1 : i + 3] == [str(ad / "mcp.json"), "--strict-mcp-config"]
    assert cc.argv[cc.argv.index("--setting-sources") + 1] == "project"
    assert json.loads(cc.files[str(ad / "mcp.json")]) == {"mcpServers": {}}
    cc2 = get_agent("claude-code").local_command(
        LocalInvocation("go", "m", ws, ad, kwargs={"setting_sources": "user,project"}, mcp_servers=srv)
    )
    assert cc2 is not None and cc2.argv[cc2.argv.index("--setting-sources") + 1] == "user,project"
    assert list(json.loads(cc2.files[str(ad / "mcp.json")])["mcpServers"]) == ["pw"]
    cur = get_agent("cursor-cli").local_command(LocalInvocation("go", "m", ws, ad))
    assert cur is not None and cur.files == {} and "--approve-mcps" not in cur.argv
    cur2 = get_agent("cursor-cli").local_command(LocalInvocation("go", "m", ws, ad, mcp_servers=srv))
    assert cur2 is not None and "--approve-mcps" in cur2.argv
    assert list(cur2.files) == [str(ws / ".cursor" / "mcp.json")]
    gen = get_agent("codex").local_command(
        LocalInvocation("go", None, ws, ad, kwargs={"local_command": ["x", "{workspace}"]})
    )
    assert gen is not None and gen.files == {}
    assert not ws.exists() and not ad.exists()


def test_claude_stream_partial_usage_without_result(tmp_path):
    """B3: no result event -> sum distinct assistant message ids once each, flagged partial."""
    u1 = {"input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": 100}
    u2 = {"input_tokens": 20, "output_tokens": 2, "cache_creation_input_tokens": 5}
    lines = [
        {"type": "assistant", "message": {"id": "m1", "usage": u1, "content": []}},
        {"type": "assistant", "message": {"id": "m1", "usage": u1, "content": []}},
        {"type": "assistant", "message": {"id": "m2", "usage": u2, "content": []}},
    ]
    p = tmp_path / "claude-code.txt"
    p.write_text("\n".join(json.dumps(x) for x in lines))
    m = parse_claude_stream(p)
    assert (m.input_tokens, m.output_tokens, m.cache_read_tokens, m.cache_write_tokens) == (30, 3, 100, 5)
    assert m.extra["usage_partial"] is True
    # with result events: their usage is summed, not overwritten, and not partial
    res = {"type": "result", "total_cost_usd": 0.1, "usage": {"input_tokens": 7, "output_tokens": 3}}
    p.write_text("\n".join(json.dumps(x) for x in [*lines, res, res]))
    m2 = parse_claude_stream(p)
    assert (m2.input_tokens, m2.output_tokens) == (14, 6) and "usage_partial" not in m2.extra
    from primase.collect import collect_trial

    trial = tmp_path / "job" / "t__1"
    (trial / "agent").mkdir(parents=True)
    (trial / "agent" / "claude-code.txt").write_text("\n".join(json.dumps(x) for x in lines))
    (trial / "result.json").write_text(json.dumps({"agent_info": {"name": "claude-code"}}))
    assert collect_trial(trial, "local").usage_partial is True


def test_local_runner_kills_agent_children_after_normal_exit(tmp_path):
    """S12: a background child the agent leaves behind does not outlive the trial."""
    import os

    from primase import local_runner

    task = tmp_path / "task"
    (task / "tests").mkdir(parents=True)
    (task / "task.toml").write_text("[agent]\ntimeout_sec = 30\n")
    (task / "instruction.md").write_text("x")
    (task / "tests" / "test.sh").write_text("exit 0\n")
    pidfile = tmp_path / "child.pid"
    spec = {
        "task": "t",
        "task_dir": str(task),
        "trial_dir": str(tmp_path / "job" / "t__1"),
        "agent": "noop",
        "agent_kwargs": {"local_command": ["bash", "-c", f"sleep 60 & echo $! > {pidfile}"]},
    }
    res = local_runner.run(spec)
    assert res["verifier_result"]["rewards"] == {"reward": 1.0}, res
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_local_validate_rejects_ignored_variant_fields_and_concurrency(local_bench):
    """S9/S12: fields the local backend would silently ignore, and concurrency > 1, fail validate."""
    cfg = _cfg(
        local_bench,
        {"a": {"dir": "shared", "image_prefix": "x-", "task_toml": {"agent": {"timeout_sec": 5, "cpus": 2}}}},
        concurrency=2,
    )
    p = plan(cfg)[0][0]
    assert p.ctx.concurrency == 2
    v = get_backend("local", p.ctx.root).validate(p.spec, p.ctx)
    bad = [m for m in v.messages if "concurrency" in m or "image_prefix" in m or "task_toml" in m]
    assert not v.ok and len(bad) == 3 and "agent.cpus" in bad[-1]


def test_container_validate_rejects_local_only_fields(local_bench):
    cfg = _cfg(
        local_bench,
        {
            "a": {
                "dir": "shared",
                "require_commands": ["sh"],
                "mcp_servers": [{"name": "pw", "command": "npx", "env": {"K": "v"}}],
            }
        },
    )
    p = plan(cfg)[0][0]
    v = get_backend("pier", p.ctx.root).validate(p.spec, p.ctx)
    assert not v.ok
    assert any("require_commands" in m for m in v.messages)
    assert any("mcp_servers[pw].env" in m for m in v.messages)


def test_cli_run_refuses_when_validation_fails(local_bench, capsys):
    from primase.cli import main

    _cfg(local_bench, {"a": _variant(local_bench, "tool-a", 1)}, concurrency=2)
    rc = main(["run", str(local_bench / "experiments" / "e.yaml")])
    assert rc == 1
    assert "BAD" in capsys.readouterr().out
    assert not (local_bench / "jobs").exists() or not any((local_bench / "jobs").rglob("result.json"))


def test_plan_rotates_arm_order(local_bench):
    """S5: each arm takes each position across (task, trial); deterministic."""
    cfg = _cfg(local_bench, {"base": {"dir": "shared"}, "treat": {"dir": "shared"}}, trials=2)
    trials = plan(cfg)[0]
    firsts = [trials[i].spec.variant for i in range(0, len(trials), 2)]
    assert firsts == ["base", "treat", "treat", "base"]  # (t1,trial1) (t2,trial1) (t1,trial2) (t2,trial2)
    assert [t.spec.trial_id for t in plan(cfg)[0]] == [t.spec.trial_id for t in trials]


def test_cursor_stream_tool_breakdown(fixtures):
    from primase.agents.cursor_cli import parse_cursor_stream as parse

    m = parse(fixtures / "jobs/anb-x-A/react-lead-form__aaa/agent/cursor-cli.txt")
    assert m.extra["tool_calls_by_name"] == {"readToolCall": 1, "shellToolCall": 1}


def test_variant_hash_tracks_referenced_file_contents(local_bench):
    """S11: editing an @file prompt or an overlay source changes variant_hash; tool_versions are recorded."""
    from primase.provenance import tool_versions, variant_hash

    v = _cfg(
        local_bench,
        {"a": {"dir": "shared", "instruction_prepend": "@prompts/tool-a.md", "overlay_files": {"s": "skills/tool-a"}}},
    ).variants["a"]
    h1 = variant_hash("a", v, local_bench)
    (local_bench / "prompts" / "tool-a.md").write_text("changed")
    h2 = variant_hash("a", v, local_bench)
    (local_bench / "skills" / "tool-a" / "SKILL.md").write_text("changed")
    h3 = variant_hash("a", v, local_bench)
    assert len({h1, h2, h3}) == 3
    tv = tool_versions([sys.executable, "definitely-not-a-tool-xyz"])
    assert tv is not None and list(tv) == [sys.executable] and tv[sys.executable].startswith("Python")
