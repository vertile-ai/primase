from pathlib import Path

from helicase.backends import (
    Backend,
    RunContext,
    get_backend,
    get_registry,
    list_backends,
    register_backend,
    reset_registry,
)
from helicase.schema import TrialSpec


class DummyBackend(Backend):
    name = "dummy"

    def build_command(self, spec: TrialSpec, ctx: RunContext) -> list[str]:
        return ["true", spec.job_name]


def test_builtin_backends_registered():
    reset_registry()
    names = list_backends()
    assert "harbor" in names and "pier" in names
    h = get_backend("harbor", Path("."))
    p = get_backend("pier", Path("."))
    assert h.name == "harbor" and p.name == "pier"


def test_register_custom_backend(tmp_path):
    reset_registry()
    register_backend("dummy", DummyBackend)
    assert "dummy" in list_backends()
    b = get_backend("dummy", tmp_path)
    spec = TrialSpec(
        experiment="e",
        suite="s",
        task="t",
        tier="low",
        backend="dummy",
        model="m",
        variant="control",
        variant_dir="control",
        trial_index=1,
        agent="none",
    )
    ctx = RunContext(
        root=tmp_path,
        jobs_dir=tmp_path / "jobs",
        task_dir=tmp_path / "task",
        model_harness_name="m",
        agent="none",
    )
    assert b.build_command(spec, ctx) == ["true", spec.job_name]
    reset_registry()
    assert "dummy" not in get_registry()


def test_unknown_backend_error():
    reset_registry()
    try:
        get_backend("nope", Path("."))
        raise AssertionError("expected KeyError")
    except KeyError as e:
        assert "nope" in str(e)
