from helicase.backends import get_backend
from helicase.backends.harbor import _force_open_agent_network
from helicase.orchestrator import plan
from helicase.toml_edit import get_table_key, set_table_key

DEEPSWE_LIKE = """schema_version = "1.3"
[task]
name = "demo/task"
[verifier]
network_mode = "no-network"
environment_mode = "separate"
timeout_sec = 1800.0
[verifier.environment]
cpus = 2
[[verifier.collect]]
command = "echo hi"
[agent]
network_mode = "no-network"
timeout_sec = 100.0
[environment]
docker_image = "example:latest"
cpus = 2
"""


def test_set_table_key_preserves_other_sections():
    text = set_table_key(DEEPSWE_LIKE, "agent", "network_mode", "public")
    assert get_table_key(text, "agent", "network_mode") == "public"
    assert get_table_key(text, "verifier", "network_mode") == "no-network"
    assert 'environment_mode = "separate"' in text
    assert get_table_key(text, "agent", "timeout_sec") == "100.0"


def test_force_open_does_not_touch_verifier():
    text, notes = _force_open_agent_network(DEEPSWE_LIKE)
    assert "agent.network_mode no-network -> public" in notes
    assert get_table_key(text, "agent", "network_mode") == "public"
    assert get_table_key(text, "verifier", "network_mode") == "no-network"
    # environment had no network_mode (Harbor default is public) – leave it
    assert get_table_key(text, "environment", "network_mode") is None


def test_force_open_environment_baseline_when_closed():
    src = set_table_key(DEEPSWE_LIKE, "environment", "network_mode", "no-network")
    text, notes = _force_open_agent_network(src)
    assert any("environment.network_mode" in n for n in notes)
    assert get_table_key(text, "environment", "network_mode") == "public"
    assert get_table_key(text, "verifier", "network_mode") == "no-network"


def test_harbor_prepare_run_overlay(bench_root, tmp_path):
    import yaml

    from helicase.config import load_config

    task = bench_root / "work/variants/tb/control/rlf"
    (task / "task.toml").write_text(DEEPSWE_LIKE)
    data = {
        "name": "net",
        "root": "..",
        "models": [{"id": "composer-2.5"}],
        "tiers": ["medium"],
        "suites": [{"id": "tb"}],
        "variants": {"control": {"dir": "control"}},
        "budget": {"allow_non_included_models": True},
        "network": "open",
    }
    p = bench_root / "experiments" / "net.yaml"
    p.write_text(yaml.safe_dump(data))
    cfg = load_config(p)
    [trial] = plan(cfg)[0]
    b = get_backend("harbor", bench_root)
    b.prepare_run(trial.spec, trial.ctx)
    assert trial.ctx.task_dir != task
    assert (trial.ctx.task_dir / "task.toml").is_file()
    assert get_table_key((trial.ctx.task_dir / "task.toml").read_text(), "agent", "network_mode") == "public"
    assert get_table_key((trial.ctx.task_dir / "task.toml").read_text(), "verifier", "network_mode") == "no-network"
    # original task is unchanged
    assert get_table_key((task / "task.toml").read_text(), "agent", "network_mode") == "no-network"
    # overlay path is what the harbor command will use
    cmd = b.build_command(trial.spec, trial.ctx)
    assert str(trial.ctx.task_dir) in cmd
