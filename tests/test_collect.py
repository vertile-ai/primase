from primase.collect import (
    collect_job,
    collect_trial,
    job_complete,
    parse_claude_stream,
    parse_codex_rollouts,
    parse_cursor_stream,
)


def test_cursor_stream(fixtures):
    s = parse_cursor_stream(fixtures / "jobs/anb-x-A/react-lead-form__aaa/agent/cursor-cli.txt")
    assert s["usage"] == {
        "inputTokens": 500,
        "outputTokens": 300,
        "cacheReadTokens": 1000,
        "cacheWriteTokens": 0,
    }
    assert s["tool_calls"] == 2 and s["n_model_calls"] == 2
    assert s["tool_output_chars"] == 40 + 3 and s["shell_output_chars"] == 40 and s["shell_calls"] == 1
    assert s["reported_cost"] is None


def test_harbor_trial_with_stream(fixtures):
    r = collect_trial(fixtures / "jobs/anb-x-A/react-lead-form__aaa", "harbor")
    assert r.status == "passed" and r.reward == 1.0
    assert (r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens) == (500, 300, 1000, 0)
    assert r.prompt_tokens_total == 1500 and r.total_tokens == 1800
    assert r.cost_source is None and r.cli_reported_cost_usd is None and r.cost_usd == 0.0012
    assert r.n_turns == 2 and r.n_tool_calls == 2
    assert r.duration_s == 600 and r.agent_s == 300 and r.verifier_s == 150
    assert r.model == "composer-2.5" and r.task == "react-lead-form"
    assert r.schema_version == "1.0"


def test_cli_reported_cost_marks_billing(fixtures):
    r = collect_trial(fixtures / "jobs/anb-x-B/react-lead-form__bbb", "harbor")
    assert r.cost_source == "cursor_cli" and r.cli_reported_cost_usd == 0.25


def test_pier_trajectory_fallback(fixtures):
    r = collect_trial(fixtures / "jobs/pier-job/yjs-map-conflict-detection__ccc", "pier")
    assert r.status == "failed" and r.partial == 0.5
    assert r.input_tokens == 100 and r.output_tokens == 50
    assert r.n_turns == 2 and r.n_tool_calls == 1 and r.tool_output_chars == 8 and r.tool_output_tokens_est == 2
    assert r.cost_source == "cursor_pricing" and r.cost_usd == 0.00009
    assert r.duration_s == 30


def test_error_trial(fixtures):
    [r] = collect_job(fixtures / "jobs/err-job", "harbor")
    assert r.status == "error" and r.exception.startswith("AgentTimeoutError")
    assert r.agent_timed_out and r.usage_missing
    assert r.total_tokens is None


def test_job_complete(fixtures, tmp_path):
    assert job_complete(fixtures / "jobs/anb-x-A")
    assert not job_complete(tmp_path / "nope")
    d = tmp_path / "j" / "t__1"
    d.mkdir(parents=True)
    (d / "result.json").write_text('{"started_at": "x"}')
    assert not job_complete(tmp_path / "j")


def test_tool_output_model_visible_approximation():
    from primase.collect import _tool_output

    shell = {"success": {"command": "npm test", "stdout": "aa", "stderr": "b", "interleavedOutput": "aab"}}
    assert _tool_output("shellToolCall", shell) == (3, 3)
    assert _tool_output("shellToolCall", {"failure": {"stdout": "x", "stderr": "yy"}}) == (3, 3)
    edit = {"success": {"diffString": "-a+b", "message": "ok", "afterFullFileContent": "X" * 9999}}
    assert _tool_output("editToolCall", edit) == (6, 0)
    assert _tool_output("readToolCall", {"success": {"content": "abcd", "relatedCursorRules": []}}) == (4, 0)


def test_claude_stream_and_trial(fixtures):
    s = parse_claude_stream(fixtures / "jobs/claude-job/demo-task__c1/agent/claude-code.txt")
    assert s["has_usage"]
    assert s["usage"] == {
        "inputTokens": 100,
        "outputTokens": 40,
        "cacheReadTokens": 200,
        "cacheWriteTokens": 8,
    }
    assert s["reported_cost"] == 0.12 and s["cost_source"] == "claude_code"
    r = collect_trial(fixtures / "jobs/claude-job/demo-task__c1", "harbor")
    assert r.status == "passed"
    assert r.input_tokens == 100 and r.output_tokens == 40
    assert r.cache_read_tokens == 200 and r.cache_write_tokens == 8
    assert r.cli_reported_cost_usd == 0.12 and r.cost_source == "claude_code"
    assert r.prompt_tokens_total == 308 and r.total_tokens == 348


def test_codex_rollout_and_trial(fixtures):
    s = parse_codex_rollouts(fixtures / "jobs/codex-job/demo-task__d1/agent")
    assert s["has_usage"]
    assert s["usage"] == {
        "inputTokens": 80,
        "outputTokens": 20,
        "cacheReadTokens": 40,
        "cacheWriteTokens": 4,
    }
    assert s["tool_calls"] == 1 and s["tool_output_chars"] == 7
    r = collect_trial(fixtures / "jobs/codex-job/demo-task__d1", "harbor")
    assert r.status == "failed" and r.partial == 0.25
    assert r.input_tokens == 80 and r.output_tokens == 20
    assert r.total_tokens == 80 + 40 + 4 + 20
