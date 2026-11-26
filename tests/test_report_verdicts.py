"""Verdict rules: usable rows, paired gate, minimum evidence, two-level CI, seeds, aggregates, notes."""

import os
import subprocess
import sys
import textwrap

import pytest

from primase.report import _seed, markdown, paired_deltas, regressions, verdicts
from primase.schema import TrialResult


def _r(task, variant, status, tokens=100, t=1, model="m", agent="claude-code", cost=None, **kw):
    return TrialResult(
        job_name=f"{task}-{variant}-{t}",
        backend="local",
        suite="s",
        task=task,
        model=model,
        variant=variant,
        trial_index=t,
        status=status,
        total_tokens=tokens,
        agent_s=10.0,
        cost_usd=cost,
        agent=agent,
        **kw,
    )


def _saving_rows(n_tasks, base=100, var=50):
    rows = []
    for i in range(n_tasks):
        rows += [_r(f"t{i}", "control", "passed", base), _r(f"t{i}", "v", "passed", var)]
    return rows


def _one(rows):
    [v] = verdicts(rows, baseline="control")
    return v


# 1. token-usable rows (B3)
def test_unusable_rows_excluded_without_fallback():
    rows = [
        _r("a", "control", "passed", 1000, agent_timed_out=True),
        _r("a", "v", "passed", 50),
        _r("b", "control", "passed", 100),
        _r("b", "v", "passed", 10, usage_partial=True),
        _r("c", "control", "passed", 100, usage_missing=True),
        _r("c", "v", "passed", 50, t=1),
        _r("c", "control", "passed", 200, t=2),
        _r("c", "v", "passed", 100, t=2),
    ]
    deltas = paired_deltas(rows, baseline="control")
    assert [(d["task"], d["n_base"], d["n_variant"]) for d in deltas] == [("c", 1, 2)]
    assert deltas[0]["total_tokens_base"] == 200
    v = _one(rows)
    assert v["excluded_usage_base"] == 2 and v["excluded_usage_variant"] == 1
    assert "| 2/1 |" in markdown(rows, "t", baseline="control")


# 2. correctness gate (B4, S1)
def test_gate_ignores_errors_and_uncommon_cells():
    rows = _saving_rows(12)
    # variant errors and a baseline-only cell must not count as variant failures
    rows += [_r("t0", "v", "error", t=2), _r("t0", "control", "failed", t=2), _r("t1", "control", "passed", t=3)]
    [reg] = regressions(rows, baseline="control")
    assert reg["gate"] == "hold" and reg["n_gate_cells"] == 12  # error share 1/13 < 10%
    assert reg["errors_variant"] == 1 and reg["errors_base"] == 0


def test_gate_baseline_errors_do_not_lower_the_bar():
    rows = []
    for i in range(6):
        rows += [_r(f"t{i}", "control", "passed"), _r(f"t{i}", "v", "failed")]
        rows += [_r(f"t{i}", "control", "error", t=2), _r(f"t{i}", "v", "failed", t=2)]
    # 50% baseline error share → withheld, even though the variant clearly lost
    [reg] = regressions(rows, baseline="control")
    assert reg["gate"] == "unclear" and "error rate" in reg["gate_reason"]
    assert reg["baseline_rate"] == 1.0 and reg["variant_rate"] == 0.0
    assert _one(rows)["verdict"] == "gate unclear"


def test_gate_regression_and_unclear_outcomes():
    rows = []
    for i in range(6):
        rows += [_r(f"t{i}", "control", "passed"), _r(f"t{i}", "v", "failed")]
    [reg] = regressions(rows, baseline="control")
    assert reg["gate"] == "REGRESSION" and reg["regression"] is True
    assert reg["gate_diff"] == -1.0 and reg["gate_ci_hi"] == -1.0
    assert _one(rows)["verdict"] == "REGRESSION"

    noisy = _saving_rows(6)
    noisy[1] = _r("t0", "v", "failed", 50)
    [reg] = regressions(noisy, baseline="control")
    assert reg["gate"] == "unclear" and reg["gate_diff"] < 0 and reg["gate_ci_hi"] >= 0
    assert reg["lost_tasks"] == ["t0"]
    v = _one(noisy)
    assert v["verdict"] == "gate unclear"  # savings not credited


def test_gate_no_common_cells_and_task_lists_in_markdown():
    rows = [_r("a", "control", "passed", t=1), _r("a", "v", "passed", t=2)]
    [reg] = regressions(rows, baseline="control")
    assert reg["gate"] == "unclear" and reg["n_gate_cells"] == 0 and reg["baseline_rate"] is None

    rows = _saving_rows(6)
    rows += [_r("t0", "control", "failed", t=2), _r("t0", "v", "passed", t=2)]
    rows += [_r("t1", "control", "passed", t=2), _r("t1", "v", "failed", t=2)]
    [reg] = regressions(rows, baseline="control")
    assert reg["unstable_baseline_tasks"] == ["t0"] and reg["lost_tasks"] == ["t1"]
    md = markdown(rows, "t", baseline="control")
    assert "lost tasks (baseline pass rate > variant): t1" in md
    assert "unstable baseline tasks (0 < baseline pass rate < 1): t0" in md


# 3. minimum evidence (B5)
def test_minimum_paired_tasks():
    assert _one(_saving_rows(5))["verdict"] == "inconclusive (n=5 paired tasks, need 6)"
    assert _one(_saving_rows(6))["verdict"] == "saves tokens"
    assert _one(_saving_rows(6, 100, 150))["verdict"] == "costs tokens"


# 4. two-level bootstrap (S2)
def test_two_level_ci_reflects_trial_variance():
    rows = []
    for i in range(6):
        for t, (b, v) in enumerate(((100, 20), (100, 50), (100, 80)), start=1):
            rows += [_r(f"t{i}", "control", "passed", b, t=t), _r(f"t{i}", "v", "passed", v, t=t)]
    v = _one(rows)
    # every task has the same -50% median delta; only trial resampling can widen the CI
    assert v["total_tokens_median"] == -50.0
    assert v["total_tokens_ci_lo"] < -50.0 < v["total_tokens_ci_hi"] < 0
    assert "| t0 | m | v | 3/3 |" in markdown(rows, "t", baseline="control")


# 5. deterministic seed (S3)
def test_seed_is_stable_across_hash_seeds():
    assert _seed("s", "m", "v", "total_tokens") == _seed("s", "m", "v", "total_tokens")
    code = textwrap.dedent(
        """
        from primase.report import markdown
        from primase.schema import TrialResult
        rows = []
        for i in range(6):
            for t in (1, 2):
                for var, tok in (("control", 100 + 7 * i + t), ("v", 60 + 13 * i - 9 * t)):
                    rows.append(TrialResult(job_name=f"{i}{var}{t}", backend="local", suite="s",
                        task=f"t{i}", model="m", variant=var, trial_index=t, status="passed", total_tokens=tok))
        print(markdown(rows, "t"))
        """
    )
    outs = [
        subprocess.run(
            [sys.executable, "-c", code], env={**os.environ, "PYTHONHASHSEED": h}, capture_output=True, check=True
        ).stdout
        for h in ("1", "2")
    ]
    assert outs[0] == outs[1] and b"[95% CI]" in outs[0]


# 6. aggregate ratio and dropped tasks (S4)
def test_aggregate_delta_and_dropped_tasks():
    rows = [
        _r("small", "control", "passed", 100),
        _r("small", "v", "passed", 50),
        _r("big", "control", "passed", 1000),
        _r("big", "v", "passed", 1100),
        _r("x", "control", "failed"),
        _r("x", "v", "passed"),
        _r("y", "control", "passed"),
        _r("y", "v", "failed"),
        _r("z", "control", "failed"),
        _r("z", "v", "failed"),
    ]
    v = _one(rows)
    assert v["total_tokens_aggregate_delta_pct"] == 100.0 * (1150 - 1100) / 1100
    assert (v["dropped_tasks_base"], v["dropped_tasks_variant"], v["dropped_tasks_both"]) == (1, 1, 1)
    md = markdown(rows, "t", baseline="control")
    assert "aggregate Δ total tokens" in md and "| 1/1/1 |" in md and "+4.5%" in md


# 7. cost column (S5)
def test_cost_delta_in_verdict_table():
    rows = []
    for i in range(6):
        rows += [_r(f"t{i}", "control", "passed", cost=1.0), _r(f"t{i}", "v", "passed", cost=0.8)]
    assert _one(rows)["cost_usd_median"] == pytest.approx(-20.0)
    md = markdown(rows, "t", baseline="control")
    assert "| Δ cost |" in md and "-20.0%" in md


# 8. cross-agent and missing-baseline notes (S6)
def test_cross_agent_note_and_missing_baseline():
    rows = [_r("a", "control", "passed", agent="claude-code"), _r("a", "v", "passed", agent="codex")]
    rows += [_r("a", "w", "passed", model="other")]
    md = markdown(rows, "t", baseline="control")
    assert "agent `codex` vs baseline agent `claude-code` — token fields are not comparable across agents." in md
    heads = {h["variant"]: h for h in verdicts(rows, baseline="control")}
    assert heads["w"]["verdict"] == "no baseline rows for model other"
    assert heads["w"]["gate"] == "no baseline"
    assert "no baseline rows for model other — not compared." in md


# 9. text-only tool-output label
def test_tool_output_label_is_text_only():
    md = markdown(_saving_rows(1), "t", baseline="control")
    assert "tool-out tok≈ (text)" in md and "images in tool results are not counted" in md
