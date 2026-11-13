"""suites.json registry: suites, task tiers, variant dirs."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TIERS = ("low", "medium", "high")


@dataclass(frozen=True)
class TaskRef:
    suite: str
    task: str
    tier: str
    runner: str


class Registry:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.root = self.path.parent
        self.data: dict[str, Any] = json.loads(self.path.read_text())
        self.suites = {s["id"]: s for s in self.data["suites"]}

    def suite(self, sid: str) -> dict[str, Any]:
        if sid not in self.suites:
            raise KeyError(f"unknown suite {sid!r} (known: {', '.join(self.suites)})")
        return self.suites[sid]

    def tasks(self, sid: str) -> list[TaskRef]:
        s = self.suite(sid)
        runner = s.get("runner", "harbor")
        if s.get("tasks"):
            return [TaskRef(sid, t, meta.get("tier", "low"), runner) for t, meta in s["tasks"].items()]
        lines = (self.root / s["tasks_file"]).read_text().splitlines()
        names = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
        tiers = s.get("task_tiers") or {}
        return [TaskRef(sid, t, tiers.get(t, s.get("default_tier", "low")), runner) for t in names]

    def select(self, sid: str, tiers: list[str], tasks: list[str] | str = "all") -> tuple[list[TaskRef], list[TaskRef]]:
        """Returns (selected, skipped_by_tier). Explicit task names never bypass the tier filter."""
        all_tasks = self.tasks(sid)
        if tasks != "all":
            known = {t.task for t in all_tasks}
            unknown = [t for t in tasks if t not in known]
            if unknown:
                raise KeyError(f"{sid}: unknown task(s) {unknown}")
            wanted = set(tasks)
            all_tasks = [t for t in all_tasks if t.task in wanted]
        sel = [t for t in all_tasks if t.tier in tiers]
        skip = [t for t in all_tasks if t.tier not in tiers]
        return sel, skip

    def variant_dir(self, sid: str, variant_dir: str, task: str) -> Path:
        return self.root / "work" / "variants" / sid / variant_dir / task


def read_task_toml(task_dir: Path) -> dict[str, Any]:
    return tomllib.loads((task_dir / "task.toml").read_text())
