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
    (d / "environment" / "workspace" / "seed.txt").write_text("seed")
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
        f'a=$(case "$(basename "$PRIMASE_TRIAL_DIR")" in t1__*) echo ABC;; *) echo XYZ;; esac); '
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
    assert (trial_dir / "workspace" / "seed.txt").exists()
    assert not (trial_dir / "workspace" / "site").exists()  # only environment/workspace is seeded
    instr = (trial_dir / "agent" / "instruction.md").read_text()
    assert instr.startswith("Use tool-a") and "{{PRIMASE_PORT}}" not in instr
    mcp = json.loads((trial_dir / "agent" / "mcp.json").read_text())
    assert mcp == {"mcpServers": {"pw": {"command": "npx", "args": ["@playwright/mcp@latest"]}}}
    # resume: second run skips completed jobs and re-collects identical rows
    rows2, _ = orch.run(trials)
    assert sorted(r.trial_id or "" for r in rows2) == sorted(r.trial_id or "" for r in rows)

    heads = {h["variant"]: h for h in verdicts(rows, baseline="tool-b")}
    assert heads["tool-a"]["verdict"] == "saves tokens"
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
