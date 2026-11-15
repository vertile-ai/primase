"""Publishable, redacted results bundles + manifest verification."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from . import __version__
from .config import ExperimentConfig, load_config
from .provenance import hash_path, primase_git_sha
from .report import markdown, write_csv
from .schema import SCHEMA_VERSION, TrialResult
from .secrets import Redactor, SecretStore

MANIFEST_VERSION = "1.0"

# paths that may contain secrets or are too large / irrelevant for a public bundle
_SKIP_NAME_PARTS = {".git", "__pycache__", ".venv", "node_modules"}


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_scrubbed(src: Path, dest: Path, redactor: Redactor) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        for child in src.rglob("*"):
            if not child.is_file() or child.is_symlink():
                continue
            if any(p in _SKIP_NAME_PARTS for p in child.parts):
                continue
            rel = child.relative_to(src)
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = child.read_bytes()
            except OSError:
                continue
            text_try = False
            try:
                text = data.decode("utf-8")
                text_try = True
            except UnicodeDecodeError:
                out.write_bytes(data)
                continue
            if text_try:
                out.write_text(redactor(text))
    elif src.is_file():
        try:
            text = src.read_text(errors="replace")
            dest.write_text(redactor(text))
        except OSError:
            shutil.copy2(src, dest)


def build_manifest(
    cfg: ExperimentConfig,
    rows: list[TrialResult],
    *,
    bundle_name: str,
) -> dict[str, Any]:
    return {
        "manifest_version": MANIFEST_VERSION,
        "schema_version": SCHEMA_VERSION,
        "primase_version": __version__,
        "primase_git_sha": primase_git_sha(),
        "created_at": datetime.now(UTC).isoformat(),
        "bundle_name": bundle_name,
        "experiment": {
            "name": cfg.name,
            "description": cfg.description,
            "config_hash": cfg.config_hash(),
            "baseline": cfg.baseline_name,
            "network": cfg.resolved_network(),
            "source": str(cfg.source_path) if cfg.source_path else None,
        },
        "variants": {name: v.model_dump(mode="json") for name, v in cfg.variants.items()},
        "models": [m.model_dump(mode="json") for m in cfg.models],
        "agent": cfg.agent.model_dump(mode="json"),
        "n_trials": len(rows),
        "trials": [
            {
                "trial_id": r.trial_id,
                "job_name": r.job_name,
                "suite": r.suite,
                "task": r.task,
                "model": r.model,
                "variant": r.variant,
                "agent": r.agent,
                "agent_version": r.agent_version,
                "status": r.status,
                "config_hash": r.config_hash,
                "variant_hash": r.variant_hash,
                "task_content_hash": r.task_content_hash,
                "docker_image": r.docker_image,
                "docker_image_digest": r.docker_image_digest,
                "network_mode": r.network_mode,
                "harbor_version": r.harbor_version,
                "pier_version": r.pier_version,
                "primase_version": r.primase_version,
                "primase_git_sha": r.primase_git_sha,
                "trajectory_path": r.trajectory_path,
                "agent_log_path": r.agent_log_path,
                "verifier_path": r.verifier_path,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            }
            for r in rows
        ],
    }


def export_bundle(
    cfg: ExperimentConfig,
    rows: list[TrialResult],
    dest: Path,
    secrets: SecretStore | None = None,
) -> Path:
    """Write a self-contained, redacted results bundle under *dest*.

    Layout::

        <dest>/
          manifest.json
          config.resolved.yaml
          trials.csv
          trials.jsonl
          summary.md
          trajectories/<trial_id>/...
    """
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    redactor = secrets.redactor if secrets else Redactor()

    write_csv(rows, dest / "trials.csv")
    with (dest / "trials.jsonl").open("w") as f:
        for r in rows:
            f.write(redactor(json.dumps(r.model_dump(mode="json"), default=str)) + "\n")

    (dest / "summary.md").write_text(redactor(markdown(rows, f"primase – {cfg.name}", cfg.baseline_name)))
    (dest / "config.resolved.yaml").write_text(redactor(yaml.safe_dump(cfg.canonical_dict(), sort_keys=False)))

    traj_root = dest / "trajectories"
    for r in rows:
        tid = r.trial_id or r.job_name
        slot = traj_root / tid
        if r.trial_dir and Path(r.trial_dir).is_dir():
            _copy_scrubbed(Path(r.trial_dir), slot, redactor)
        else:
            # copy individual known artifacts if absolute paths exist
            for label, p in (
                ("trajectory.json", r.trajectory_path),
                ("agent_log", r.agent_log_path),
                ("verifier", r.verifier_path),
            ):
                if p and Path(p).exists():
                    target = slot / label
                    _copy_scrubbed(Path(p), target, redactor)

    manifest = build_manifest(cfg, rows, bundle_name=dest.name)
    # content hashes of exported files (manifest written after first pass, then rehashed)
    files: dict[str, str] = {}
    for path in sorted(dest.rglob("*")):
        if path.is_file():
            files[path.relative_to(dest).as_posix()] = _file_sha256(path)
    manifest["files"] = files
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n")
    files["manifest.json"] = _file_sha256(dest / "manifest.json")
    manifest["files"] = files
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n")
    return dest


def verify_bundle(bundle: Path, *, check_config: Path | None = None) -> list[str]:
    """Validate a results bundle. Returns a list of problems (empty = ok)."""
    bundle = Path(bundle)
    problems: list[str] = []
    man_path = bundle / "manifest.json"
    if not man_path.exists():
        return ["missing manifest.json"]
    try:
        manifest = json.loads(man_path.read_text())
    except ValueError as e:
        return [f"manifest.json parse error: {e}"]

    for req in ("manifest_version", "experiment", "trials", "files"):
        if req not in manifest:
            problems.append(f"manifest missing key {req!r}")

    files = manifest.get("files") or {}
    for rel, expected in files.items():
        if rel == "manifest.json":
            continue  # self-hash changes when we rewrite; skip strict check on itself
        p = bundle / rel
        if not p.exists():
            problems.append(f"missing file {rel}")
            continue
        actual = _file_sha256(p)
        if actual != expected:
            problems.append(f"hash mismatch {rel}: expected {expected[:12]}… got {actual[:12]}…")

    if check_config is not None:
        cfg = load_config(check_config)
        expected = manifest.get("experiment", {}).get("config_hash")
        actual = cfg.config_hash()
        if expected and expected != actual:
            problems.append(f"config_hash mismatch: manifest={expected} config={actual}")

    # basic trial integrity
    for t in manifest.get("trials") or []:
        tid = t.get("trial_id") or t.get("job_name")
        if not tid:
            problems.append("trial entry missing trial_id")
            continue
        slot = bundle / "trajectories" / tid
        if not slot.exists():
            problems.append(f"missing trajectories/{tid}")

    return problems


def load_rows_from_bundle(bundle: Path) -> list[TrialResult]:
    rows: list[TrialResult] = []
    jl = Path(bundle) / "trials.jsonl"
    if not jl.exists():
        return rows
    for line in jl.read_text().splitlines():
        if not line.strip():
            continue
        rows.append(TrialResult.model_validate(json.loads(line)))
    return rows


def bundle_tree_hash(bundle: Path) -> str | None:
    return hash_path(Path(bundle))
