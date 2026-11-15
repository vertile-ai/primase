"""Export / verify results bundles (redacted, self-contained)."""

import json
import stat

import yaml

from primase.config import load_config
from primase.export import export_bundle, verify_bundle
from primase.orchestrator import Orchestrator, plan
from primase.schema import TrialResult
from primase.secrets import REDACTED, SecretStore

FAKE = "fake_test_secret_0123456789abcdef"


def test_export_and_verify_roundtrip(bench_root, tmp_path, fixtures):
    data = {
        "name": "exp-bundle",
        "root": "..",
        "models": [{"id": "composer-2.5"}],
        "tiers": ["low"],
        "suites": [{"id": "ds"}],
        "trials": 1,
        "variants": {
            "control": {"dir": "control"},
            "treatment": {"dir": "treatment"},
        },
        "baseline": "control",
        "agent": {"plugin": {"allow_non_included_models": True}},
        "budget": {},
        "secrets": ["CURSOR_API_KEY"],
    }
    p = bench_root / "experiments" / "bundle.yaml"
    p.write_text(yaml.safe_dump(data))
    cfg = load_config(p)

    # fabricate two trial results with provenance + a secret in a trajectory
    rows = []
    for variant in ("control", "treatment"):
        trial_dir = tmp_path / "jobs" / f"job-{variant}" / "task-a__1"
        (trial_dir / "agent").mkdir(parents=True)
        (trial_dir / "verifier").mkdir()
        (trial_dir / "agent" / "trajectory.json").write_text('{"steps":[]}')
        (trial_dir / "agent" / "cursor-cli.txt").write_text(f"key={FAKE}\nok\n")
        (trial_dir / "verifier" / "reward.txt").write_text("1")
        rows.append(
            TrialResult(
                job_name=f"job-{variant}",
                trial_dir=str(trial_dir),
                backend="pier",
                suite="ds",
                task="task-a",
                model="composer-2.5",
                variant=variant,
                trial_index=1,
                agent="cursor-cli",
                status="passed",
                reward=1.0,
                trial_id=f"tid-{variant}",
                config_hash=cfg.config_hash(),
                total_tokens=100 if variant == "control" else 50,
                trajectory_path=str(trial_dir / "agent" / "trajectory.json"),
                agent_log_path=str(trial_dir / "agent" / "cursor-cli.txt"),
                verifier_path=str(trial_dir / "verifier"),
            )
        )

    secrets = SecretStore.load(["CURSOR_API_KEY"], None, environ={"CURSOR_API_KEY": FAKE})
    dest = tmp_path / "bundle"
    export_bundle(cfg, rows, dest, secrets=secrets)

    assert (dest / "manifest.json").exists()
    assert (dest / "trials.csv").exists()
    assert (dest / "summary.md").exists()
    assert (dest / "config.resolved.yaml").exists()
    man = json.loads((dest / "manifest.json").read_text())
    assert man["experiment"]["config_hash"] == cfg.config_hash()
    assert man["n_trials"] == 2
    # secret scrubbed from copied trajectory
    leaked = list(dest.rglob("cursor-cli.txt"))
    assert leaked
    assert FAKE not in leaked[0].read_text()
    assert REDACTED in leaked[0].read_text()

    problems = verify_bundle(dest, check_config=p)
    assert problems == []

    # tamper → verify fails
    (dest / "summary.md").write_text("tampered\n")
    problems = verify_bundle(dest)
    assert any("hash mismatch" in p for p in problems)


def test_export_after_fake_run(bench_root, monkeypatch):
    fake = bench_root / ".venv" / "bin" / "pier"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "a = sys.argv\n"
        "job = a[a.index('--job-name') + 1]\n"
        "out = a[a.index('-o') + 1]\n"
        "d = os.path.join(out, job, 'task-a__x')\n"
        "os.makedirs(os.path.join(d, 'agent'), exist_ok=True)\n"
        "open(os.path.join(d, 'agent', 'cursor-cli.txt'), 'w').write('ok\\n')\n"
        "json.dump({\n"
        "    'task_name': 'x/task-a',\n"
        "    'verifier_result': {'rewards': {'reward': 1}},\n"
        "    'started_at': '2026-11-13T00:00:00Z',\n"
        "    'finished_at': '2026-11-13T00:01:00Z',\n"
        "    'agent_info': {'name': 'cursor-cli', 'version': '1.2.3'},\n"
        "}, open(os.path.join(d, 'result.json'), 'w'))\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    data = {
        "name": "exp-run-export",
        "root": "..",
        "models": [{"id": "composer-2.5"}],
        "tiers": ["low"],
        "suites": [{"id": "ds"}],
        "trials": 1,
        "variants": {"control": {"dir": "control"}},
        "agent": {"plugin": {"allow_non_included_models": True}},
        "budget": {"stop_on_billed_cost": False},
        "secrets": [],
    }
    p = bench_root / "experiments" / "runexp.yaml"
    p.write_text(yaml.safe_dump(data))
    cfg = load_config(p)
    trials = [t for t in plan(cfg)[0] if t.spec.variant == "control"][:1]
    secrets = SecretStore.load([], None, environ={})
    orch = Orchestrator(cfg, secrets)
    rows, abort = orch.run(trials)
    assert abort is None and rows and rows[0].primase_version
    assert rows[0].config_hash == cfg.config_hash()
    dest = cfg.results_path / "bundle"
    export_bundle(cfg, rows, dest, secrets=secrets)
    assert verify_bundle(dest, check_config=p) == []
