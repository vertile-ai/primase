from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    return FIX


@pytest.fixture
def bench_root(tmp_path: Path) -> Path:
    """A minimal benchmark/ tree: suites.json + variants + a fake .venv/bin/{harbor,pier}."""
    import json

    root = tmp_path / "benchmark"
    (root / ".venv" / "bin").mkdir(parents=True)
    for b in ("harbor", "pier"):
        (root / ".venv" / "bin" / b).write_text("#!/bin/sh\n")
    (root / "suites").mkdir()
    (root / "suites" / "ds.txt").write_text("# c\ntask-a\ntask-b\n")
    (root / "adapters").mkdir()
    (root / "adapters" / "compose-no-egress.yaml").write_text("services: {}\n")
    reg = {
        "suites": [
            {
                "id": "ds",
                "runner": "pier",
                "tasks_file": "suites/ds.txt",
                "default_tier": "low",
                "task_tiers": {"task-b": "high"},
            },
            {"id": "tb", "runner": "harbor", "tasks": {"rlf": {"tier": "medium"}}},
        ]
    }
    (root / "suites.json").write_text(json.dumps(reg))
    for suite, tasks in (("ds", ["task-a", "task-b"]), ("tb", ["rlf"])):
        for variant in ("control", "treatment"):
            for t in tasks:
                d = root / "work" / "variants" / suite / variant / t
                (d / "environment").mkdir(parents=True)
                (d / "environment" / "Dockerfile").write_text("FROM node:20\n")
                if variant == "treatment":
                    (d / "environment" / "extra-layer").mkdir()
                (d / "task.toml").write_text("[agent]\ntimeout_sec = 100.0\n[environment]\ncpus = 1\n")
    (root / "experiments").mkdir()
    return root
