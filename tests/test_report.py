from primase.collect import collect_trial
from primase.report import markdown, paired_deltas, regressions, write_csv
from primase.schema import TrialResult


def _r(task, variant, status, tokens, t=1, agent_s=100.0):
    return TrialResult(
        job_name=f"{task}-{variant}-{t}",
        backend="harbor",
        suite="s",
        task=task,
        model="m",
        variant=variant,
        trial_index=t,
        status=status,
        total_tokens=tokens,
        agent_s=agent_s,
    )


def test_regression_flag():
    rows = [
        _r("a", "control", "passed", 100),
        _r("b", "control", "passed", 100),
        _r("a", "treatment", "passed", 50),
        _r("b", "treatment", "failed", 10),
    ]
    [reg] = regressions(rows, baseline="control")
    assert reg["regression"] and reg["baseline_rate"] == 1.0 and reg["variant_rate"] == 0.5


def test_deltas_only_over_both_pass_and_medians():
    rows = [
        _r("a", "control", "passed", 100, 1),
        _r("a", "control", "passed", 300, 2),
        _r("a", "control", "failed", 9999, 3),
        _r("a", "treatment", "passed", 100, 1),
        _r("a", "treatment", "passed", 100, 2),
        _r("b", "control", "passed", 100),
        _r("b", "treatment", "failed", 1),
    ]
    [d] = paired_deltas(rows, baseline="control")
    assert d["task"] == "a" and d["total_tokens_base"] == 200 and d["total_tokens_variant"] == 100
    assert d["total_tokens_delta_pct"] == -50.0


def test_markdown_and_csv(fixtures, tmp_path):
    a = collect_trial(fixtures / "jobs/anb-x-A/react-lead-form__aaa", "harbor").model_copy(
        update={"variant": "control", "suite": "tb"}
    )
    b = collect_trial(fixtures / "jobs/anb-x-B/react-lead-form__bbb", "harbor").model_copy(
        update={"variant": "treatment", "suite": "tb"}
    )
    md = markdown([a, b], "t", baseline="control")
    assert "| tb | composer-2.5 | treatment | 100% | 100% | ok |" in md
    assert "-50.0%" in md  # 1800 → 900 tokens
    p = write_csv([a, b], tmp_path / "x.csv")
    assert p.read_text().splitlines()[0].startswith("schema_version,experiment,trial_id,job_name")


def test_no_deltas_message():
    assert "deltas withheld" in markdown(
        [_r("a", "control", "failed", 1), _r("a", "treatment", "passed", 1)],
        "t",
        baseline="control",
    )
