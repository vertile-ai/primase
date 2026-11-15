import stat

import pytest
import yaml

from primase.backends import get_backend
from primase.config import NETWORK_ENV, load_config
from primase.orchestrator import Orchestrator, plan
from primase.secrets import SecretStore

FAKE = "fake_test_secret_0123456789abcdef"


def _cfg(root, environ=None, **over):
    data = {
        "name": "exp",
        "root": "..",
        "models": [{"id": "composer-2.5"}, {"id": "grok-4.7-high"}],
        "tiers": ["low"],
        "suites": [{"id": "ds"}, {"id": "tb", "tasks": ["rlf"]}],
        "trials": 2,
        "variants": {
            "control": {"dir": "control", "description": "baseline"},
            "treatment": {"dir": "treatment", "require_paths": ["environment/extra-layer"]},
        },
        "baseline": "control",
        "agent": {"name": "cursor-cli", "plugin": {"included_models": ["composer-2.5", "grok-4.7-*"]}},
        "budget": {},
    }
    data.update(over)
    p = root / "experiments" / "e.yaml"
    p.write_text(yaml.safe_dump(data))
    return load_config(p, environ=environ)


def test_plan_tiers_and_ids(bench_root):
    cfg = _cfg(bench_root)
    trials, notes = plan(cfg)
    # ds: task-a (low) only; task-b high and tb/rlf medium are skipped
    assert {p.spec.task for p in trials} == {"task-a"}
    assert len(trials) == 1 * 2 * 2 * 2
    assert any("task-b" in n for n in notes) and any("rlf" in n for n in notes)
    ids = [p.spec.trial_id for p in trials]
    assert len(set(ids)) == len(ids)
    assert ids == [p.spec.trial_id for p in plan(cfg)[0]]  # deterministic
    assert trials[0].spec.backend == "pier"
    assert {p.spec.variant for p in trials} == {"control", "treatment"}
    assert cfg.resolved_network() == "open"  # default


def test_model_not_included_rejected(bench_root):
    with pytest.raises(Exception, match="included_models"):
        _cfg(bench_root, models=[{"id": "claude-opus-5-high"}])


def test_legacy_budget_included_models_migrates(bench_root):
    """Old budget.included_models / stop_on_credit_billing still load via migration."""
    cfg = _cfg(
        bench_root,
        agent={"name": "cursor-cli"},
        budget={
            "included_models": ["composer-2.5", "grok-4.7-*"],
            "stop_on_credit_billing": True,
        },
    )
    assert cfg.agent.plugin["included_models"] == ["composer-2.5", "grok-4.7-*"]
    assert cfg.budget.stop_on_billed_cost is True


def test_unknown_key_rejected(bench_root):
    with pytest.raises(Exception, match="trajectories|extra|trails|forbidden|validation"):
        _cfg(bench_root, trails=3)


def test_network_default_open_and_restricted_compose(bench_root):
    cfg = _cfg(bench_root, tiers=["medium"], suites=[{"id": "tb"}])
    [p, *_] = plan(cfg)[0]
    cmd = get_backend("harbor", bench_root).build_command(p.spec, p.ctx)
    assert "--extra-docker-compose" not in cmd and "cursor/composer-2.5" in cmd and p.spec.job_name in cmd
    assert p.ctx.network == "open"

    cfg2 = _cfg(
        bench_root,
        tiers=["medium"],
        suites=[{"id": "tb"}],
        network="restricted",
        harbor_restricted_compose="adapters/compose-no-egress.yaml",
    )
    [p2, *_] = plan(cfg2)[0]
    cmd2 = get_backend("harbor", bench_root).build_command(p2.spec, p2.ctx)
    assert "--extra-docker-compose" in cmd2
    assert "compose-no-egress.yaml" in " ".join(cmd2)
    assert p2.ctx.network == "restricted"


def test_network_env_override(bench_root):
    cfg = _cfg(
        bench_root,
        tiers=["medium"],
        suites=[{"id": "tb"}],
        network="open",
        harbor_restricted_compose="adapters/compose-no-egress.yaml",
        environ={NETWORK_ENV: "restricted"},
    )
    # YAML says open; env overrides when planning
    [p, *_] = plan(cfg, environ={NETWORK_ENV: "restricted"})[0]
    assert p.ctx.network == "restricted"
    cmd = get_backend("harbor", bench_root).build_command(p.spec, p.ctx)
    assert "--extra-docker-compose" in cmd

    with pytest.raises(ValueError, match="PRIMASE_NETWORK"):
        _cfg(bench_root, environ={NETWORK_ENV: "maybe"})


def test_legacy_arms_alias(bench_root):
    data = {
        "name": "legacy",
        "root": "..",
        "models": [{"id": "composer-2.5"}],
        "tiers": ["low"],
        "suites": [{"id": "ds"}],
        "arms": {"A": {"variant": "control"}, "B": {"variant": "treatment"}},
        "baseline_arm": "A",
        "budget": {},
        "agent": {"name": "cursor-cli", "plugin": {"allow_non_included_models": True}},
    }
    p = bench_root / "experiments" / "legacy.yaml"
    p.write_text(yaml.safe_dump(data))
    cfg = load_config(p)
    assert set(cfg.variants) == {"A", "B"}
    assert cfg.variants["A"].directory_name("A") == "control"
    assert cfg.baseline == "A"
    trials, _ = plan(cfg)
    assert {t.spec.variant for t in trials} == {"A", "B"}
    assert {t.spec.variant_dir for t in trials} == {"control", "treatment"}


def test_run_resume_scrub_and_budget(bench_root, monkeypatch, fixtures):
    """Fake runner: writes a trial dir that leaks the key and reports a billed cost → scrubbed + budget abort."""
    fake = bench_root / ".venv" / "bin" / "pier"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "a = sys.argv\n"
        "job = a[a.index('--job-name') + 1]\n"
        "out = a[a.index('-o') + 1]\n"
        "d = os.path.join(out, job, 'task-a__x', 'agent')\n"
        "os.makedirs(d, exist_ok=True)\n"
        f"assert os.environ['CURSOR_API_KEY'] == '{FAKE}'\n"
        "open(os.path.join(d, 'cursor-cli.txt'), 'w').write(\n"
        "    'env dump CURSOR_API_KEY=' + os.environ['CURSOR_API_KEY'] + '\\n'\n"
        "    + json.dumps({\n"
        "        'type': 'result', 'subtype': 'success', 'duration_ms': 1,\n"
        "        'duration_api_ms': 1, 'is_error': False, 'result': '', 'session_id': 's',\n"
        "        'usage': {\n"
        "            'inputTokens': 1, 'outputTokens': 1, 'cacheReadTokens': 0,\n"
        "            'cacheWriteTokens': 0,\n"
        "            'totalCost': float(os.environ.get('FAKE_COST', '0')),\n"
        "        },\n"
        "    }) + '\\n')\n"
        "dest = os.path.join(out, job, 'task-a__x', 'result.json')\n"
        "json.dump({\n"
        "    'task_name': 'x/task-a',\n"
        "    'verifier_result': {'rewards': {'reward': 1}},\n"
        "    'started_at': '2026-11-13T00:00:00Z',\n"
        "    'finished_at': '2026-11-13T00:01:00Z',\n"
        "}, open(dest, 'w'))\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    cfg = _cfg(
        bench_root,
        trials=1,
        models=[{"id": "composer-2.5"}],
        secrets=["CURSOR_API_KEY"],
    )
    trials = plan(cfg)[0]
    secrets = SecretStore.load(["CURSOR_API_KEY"], None, environ={"CURSOR_API_KEY": FAKE})
    orch = Orchestrator(cfg, secrets)
    rows, abort = orch.run(trials[:1])
    assert abort is None and rows[0].status == "passed" and rows[0].secret_files_scrubbed == 1
    for f in list(cfg.jobs_path.rglob("*")) + list(cfg.results_path.rglob("*")):
        if f.is_file():
            assert FAKE not in f.read_text(), f
    marker = next(cfg.jobs_path.rglob("cursor-cli.txt"))
    before = marker.stat().st_mtime_ns
    orch.run(trials[:1])
    assert marker.stat().st_mtime_ns == before
    monkeypatch.setenv("FAKE_COST", "0.5")
    rows, abort = Orchestrator(cfg, secrets).run(trials[1:2])
    assert abort and "billed" in abort
    ledger = (cfg.results_path / "ledger.jsonl").read_text()
    assert "budget_abort" in ledger and FAKE not in ledger
