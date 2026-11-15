"""Variant-level agent / model / task.toml overrides."""

import yaml

from primase.backends import get_backend
from primase.config import load_config
from primase.orchestrator import plan
from primase.toml_edit import get_table_key


def _write(bench_root, data):
    p = bench_root / "experiments" / "ovr.yaml"
    p.write_text(yaml.safe_dump(data))
    return load_config(p)


def test_variant_overrides_agent_and_model(bench_root):
    cfg = _write(
        bench_root,
        {
            "name": "ovr",
            "root": "..",
            "models": [{"id": "composer-2.5"}],
            "tiers": ["low"],
            "suites": [{"id": "ds"}],
            "budget": {},
            "agent": {
                "name": "cursor-cli",
                "kwargs": {"mode": "ask"},
                "plugin": {"allow_non_included_models": True},
            },
            "variants": {
                "control": {"dir": "control"},
                "claude": {
                    "dir": "control",
                    "agent": {"name": "claude-code", "kwargs": {"config": "/tmp/a.json"}},
                    "model": {"id": "claude-opus", "provider_prefix": "anthropic"},
                },
                "config-b": {
                    "dir": "treatment",
                    "agent": {"kwargs": {"mode": "plan"}},
                },
            },
            "baseline": "control",
        },
    )
    trials, _ = plan(cfg)
    by = {t.spec.variant: t for t in trials}
    assert by["control"].spec.agent == "cursor-cli"
    assert by["control"].ctx.agent_kwargs["mode"] == "ask"
    assert by["control"].spec.model == "composer-2.5"

    assert by["claude"].spec.agent == "claude-code"
    assert by["claude"].spec.model == "claude-opus"
    assert by["claude"].ctx.model_harness_name == "anthropic/claude-opus"
    assert by["claude"].ctx.agent_kwargs["config"] == "/tmp/a.json"

    # kwargs merge: variant overrides mode, keeps nothing else from default unless set
    assert by["config-b"].spec.agent == "cursor-cli"
    assert by["config-b"].ctx.agent_kwargs["mode"] == "plan"
    # only one model for claude variant (not crossed with top-level models)
    assert len([t for t in trials if t.spec.variant == "claude"]) == 1


def test_variant_task_toml_overlay(bench_root):
    cfg = _write(
        bench_root,
        {
            "name": "toml-ovr",
            "root": "..",
            "models": [{"id": "composer-2.5"}],
            "tiers": ["medium"],
            "suites": [{"id": "tb"}],
            "agent": {"plugin": {"allow_non_included_models": True}},
            "budget": {},
            "network": "open",
            "variants": {
                "control": {"dir": "control"},
                "long": {
                    "dir": "control",
                    "task_toml": {"agent": {"timeout_sec": 600.0}, "environment": {"cpus": 4}},
                },
            },
            "baseline": "control",
        },
    )
    trials, _ = plan(cfg)
    long = next(t for t in trials if t.spec.variant == "long")
    b = get_backend("harbor", bench_root)
    b.prepare_run(long.spec, long.ctx)
    text = (long.ctx.task_dir / "task.toml").read_text()
    assert get_table_key(text, "agent", "timeout_sec") == "600.0"
    assert get_table_key(text, "environment", "cpus") == "4"
    assert get_table_key(text, "agent", "network_mode") == "public"


def test_variant_model_string_lookup(bench_root):
    cfg = _write(
        bench_root,
        {
            "name": "mstr",
            "root": "..",
            "models": [
                {"id": "composer-2.5", "provider_prefix": "cursor"},
                {"id": "grok-4.7-high", "provider_prefix": "cursor"},
            ],
            "tiers": ["low"],
            "suites": [{"id": "ds"}],
            "agent": {"plugin": {"allow_non_included_models": True}},
            "budget": {},
            "variants": {
                "a": {"dir": "control", "model": "composer-2.5"},
                "b": {"dir": "treatment", "model": "grok-4.7-high"},
            },
            "baseline": "a",
        },
    )
    trials, _ = plan(cfg)
    assert {(t.spec.variant, t.spec.model) for t in trials} == {("a", "composer-2.5"), ("b", "grok-4.7-high")}
