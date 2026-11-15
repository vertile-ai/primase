"""Reproducibility metadata: versions, content hashes, digests, artifact paths."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import __version__
from .config import ExperimentConfig, VariantSpec
from .registry import read_task_toml


def _run(cmd: list[str]) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip() or None


def primase_git_sha() -> str | None:
    """Best-effort git SHA of the primase install / cwd checkout."""
    return _run(["git", "rev-parse", "HEAD"])


def backend_version(binary: str | Path) -> str | None:
    """Ask a Harbor/Pier binary for ``--version``."""
    path = Path(binary)
    if not path.exists():
        which = shutil.which(str(binary))
        if not which:
            return None
        path = Path(which)
    out = _run([str(path), "--version"]) or _run([str(path), "version"])
    if not out:
        return None
    # first line, trim common prefixes
    line = out.splitlines()[0].strip()
    for prefix in ("harbor ", "pier ", "Harbor ", "Pier "):
        if line.lower().startswith(prefix.lower()):
            return line[len(prefix) :].strip()
    return line


def hash_path(path: Path) -> str | None:
    """SHA-256 of a file, or a stable tree hash for a directory (file paths + contents)."""
    path = Path(path)
    if not path.exists():
        return None
    h = hashlib.sha256()
    if path.is_file():
        h.update(path.read_bytes())
        return h.hexdigest()[:16]
    files = sorted(p for p in path.rglob("*") if p.is_file() and not p.is_symlink())
    for p in files:
        rel = p.relative_to(path).as_posix().encode()
        h.update(rel)
        h.update(b"\0")
        try:
            h.update(p.read_bytes())
        except OSError:
            continue
        h.update(b"\0")
    return h.hexdigest()[:16]


def variant_hash(name: str, variant: VariantSpec) -> str:
    blob = json.dumps({"name": name, **variant.model_dump(mode="json")}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def task_content_hash(task_dir: Path) -> str | None:
    """Hash of task.toml + instruction/tests/environment sources (excluding large caches)."""
    task_dir = Path(task_dir)
    if not task_dir.is_dir():
        return None
    h = hashlib.sha256()
    interesting = []
    for name in ("task.toml", "instruction.md", "instruction.txt", "tests", "environment", "solution"):
        p = task_dir / name
        if p.exists():
            interesting.append(p)
    if not interesting:
        return hash_path(task_dir)
    for p in sorted(interesting, key=lambda x: x.as_posix()):
        if p.is_file():
            h.update(p.name.encode())
            h.update(p.read_bytes())
        else:
            sub = hash_path(p)
            h.update(p.name.encode())
            h.update((sub or "").encode())
    return h.hexdigest()[:16]


def docker_image_digest(image: str) -> str | None:
    if not image:
        return None
    out = _run(["docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", image])
    if out and "@" in out:
        return out.split("@", 1)[1]
    # fall back to Id
    return _run(["docker", "image", "inspect", "--format", "{{.Id}}", image])


def read_docker_image(task_dir: Path) -> str | None:
    try:
        toml = read_task_toml(task_dir)
    except Exception:  # noqa: BLE001
        return None
    img = (toml.get("environment") or {}).get("docker_image")
    return str(img) if img else None


def artifact_paths(trial_dir: Path) -> dict[str, str | None]:
    trial_dir = Path(trial_dir)
    agent = trial_dir / "agent"
    traj = agent / "trajectory.json"
    verifier = trial_dir / "verifier"
    # prefer known agent log names
    log: Path | None = None
    for name in ("cursor-cli.txt", "claude-code.txt", "noop.json"):
        cand = agent / name
        if cand.exists():
            log = cand
            break
    if log is None and agent.exists():
        txts = sorted(agent.glob("*.txt"))
        log = txts[0] if txts else None
    return {
        "trajectory_path": str(traj) if traj.exists() else None,
        "agent_log_path": str(log) if log else None,
        "verifier_path": str(verifier) if verifier.exists() else None,
    }


def build_provenance(
    *,
    cfg: ExperimentConfig,
    variant_name: str,
    variant: VariantSpec,
    task_dir: Path,
    trial_dir: Path | None,
    backend: str,
    network: str,
    harbor_bin: Path | None = None,
    pier_bin: Path | None = None,
) -> dict[str, Any]:
    """Assemble provenance fields for a TrialResult."""
    img = read_docker_image(task_dir)
    paths = artifact_paths(trial_dir) if trial_dir else {}
    return {
        "primase_version": __version__,
        "primase_git_sha": primase_git_sha(),
        "harbor_version": (
            backend_version(harbor_bin) if harbor_bin else (backend_version("harbor") if backend == "harbor" else None)
        ),
        "pier_version": (
            backend_version(pier_bin) if pier_bin else (backend_version("pier") if backend == "pier" else None)
        ),
        "config_hash": cfg.config_hash(),
        "variant_def": {"name": variant_name, **variant.model_dump(mode="json")},
        "variant_hash": variant_hash(variant_name, variant),
        "task_content_hash": task_content_hash(task_dir),
        "docker_image": img,
        "docker_image_digest": docker_image_digest(img) if img else None,
        "network_mode": network,
        **paths,
    }
