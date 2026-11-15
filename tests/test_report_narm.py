"""N-arm reports: every variant paired against baseline + bootstrap CI."""

from primase.report import bootstrap_ci, markdown, paired_deltas, regressions, summary_delta_cis
from primase.schema import TrialResult


def _r(task, variant, status, tokens, t=1, agent_s=100.0, model="m", agent="cursor-cli"):
    return TrialResult(
        job_name=f"{task}-{variant}-{t}",
        backend="harbor",
        suite="s",
        task=task,
        model=model,
        variant=variant,
        trial_index=t,
        status=status,
        total_tokens=tokens,
        agent_s=agent_s,
        agent=agent,
    )


def test_three_arm_regressions_and_deltas():
    rows = [
        _r("a", "control", "passed", 100),
        _r("b", "control", "passed", 200),
        _r("a", "v1", "passed", 50),
        _r("b", "v1", "passed", 100),
        _r("a", "v2", "failed", 10),
        _r("b", "v2", "passed", 150),
    ]
    regs = {r["variant"]: r for r in regressions(rows, baseline="control")}
    assert regs["v1"]["regression"] is False
    assert regs["v2"]["regression"] is True  # 50% vs 100%
    deltas = paired_deltas(rows, baseline="control")
    # v1: both tasks both-pass; v2: only task b both-pass
    assert {(d["variant"], d["task"]) for d in deltas} == {("v1", "a"), ("v1", "b"), ("v2", "b")}
    v1a = next(d for d in deltas if d["variant"] == "v1" and d["task"] == "a")
    assert v1a["total_tokens_delta_pct"] == -50.0


def test_bootstrap_ci_deterministic():
    vals = [10.0, 20.0, 30.0, 40.0, 50.0]
    a = bootstrap_ci(vals, seed=1)
    b = bootstrap_ci(vals, seed=1)
    assert a == b
    med, lo, hi = a
    assert med == 30.0 and lo is not None and hi is not None and lo <= med <= hi


def test_summary_cis_and_markdown_narm():
    rows = []
    for task, bt, vt in (("a", 100, 50), ("b", 200, 100), ("c", 80, 40)):
        rows += [
            _r(task, "control", "passed", bt),
            _r(task, "fast", "passed", vt, agent="claude-code"),
        ]
    deltas = paired_deltas(rows, baseline="control")
    cis = summary_delta_cis(deltas)
    assert len(cis) == 1
    assert cis[0]["median"] == -50.0
    assert cis[0]["ci_lo"] is not None
    md = markdown(rows, "narm", baseline="control")
    assert "Baseline: `control`" in md
    assert "claude-code" in md
    assert "95% CI" in md
