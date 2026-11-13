import pytest
import yaml

from helicase.backends import get_backend
from helicase.config import VariantSpec, load_config
from helicase.orchestrator import plan
from helicase.registry import Registry


def test_registry_select_and_variant_dir(bench_root):
    reg = Registry(bench_root / "suites.json")
    sel, skip = reg.select("ds", ["low"])
    assert [t.task for t in sel] == ["task-a"]
    assert [t.task for t in skip] == ["task-b"]
    with pytest.raises(KeyError, match="unknown task"):
        reg.select("ds", ["low"], ["nope"])
    path = reg.variant_dir("ds", "control", "task-a")
    assert path == bench_root / "work" / "variants" / "ds" / "control" / "task-a"
    assert path.joinpath("task.toml").exists()


def test_variant_dir_env_hooks_and_require_paths(bench_root):
    data = {
        "name": "vtest",
        "root": "..",
        "models": [{"id": "composer-2.5"}],
        "tiers": ["low"],
        "suites": [{"id": "ds"}],
        "secrets": [],
        "budget": {"allow_non_included_models": True},
        "variants": {
            "control": {"dir": "control"},
            "treatment": {
                "dir": "treatment",
                "env": {"NODE_OPTIONS": "--require /opt/extra/preload.js"},
                "setup": ["echo setup"],
                "prebuild": ["echo prebuild"],
                "require_paths": ["environment/extra-layer"],
                "image_prefix": "extra-",
            },
        },
        "baseline": "control",
    }
    p = bench_root / "experiments" / "v.yaml"
    p.write_text(yaml.safe_dump(data))
    cfg = load_config(p)
    assert cfg.variants["treatment"].directory_name("treatment") == "treatment"
    trials, _ = plan(cfg)
    treat = [t for t in trials if t.spec.variant == "treatment"]
    assert treat
    ctx = treat[0].ctx
    assert ctx.variant_env["NODE_OPTIONS"].startswith("--require")
    assert ctx.setup == ["echo setup"] and ctx.prebuild == ["echo prebuild"]
    assert ctx.require_paths == ["environment/extra-layer"]
    assert ctx.image_prefix == "extra-"
    v = get_backend("pier", bench_root).validate(treat[0].spec, ctx)
    assert v.ok, v.messages

    # missing required path fails validate
    missing_dir = ctx.task_dir / "environment" / "extra-layer"
    missing_dir.rename(missing_dir.with_name("extra-layer.bak"))
    v2 = get_backend("pier", bench_root).validate(treat[0].spec, ctx)
    assert not v2.ok and any("extra-layer" in m for m in v2.messages)


def test_variant_spec_defaults():
    v = VariantSpec()
    assert v.directory_name("foo") == "foo"
    v2 = VariantSpec(dir="other")
    assert v2.directory_name("foo") == "other"
