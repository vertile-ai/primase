"""Run one trial on the host. Invoked by the ``local`` backend as ``python -m primase.local_runner spec.json``.

Layout written (Harbor-compatible, so :mod:`primase.collect` needs no special case)::

    <job>/<task>__<trial_id>/
        result.json            started/finished, agent_execution, verifier_result.rewards, exception_info,
                               workspace (path of the agent's cwd)
        agent/<cli>.txt        agent stdout (e.g. claude-code.txt stream-json)
        agent/stderr.txt
        agent/instruction.md   exact prompt given to the agent
        agent/command.json     argv (instruction elided)
        agent/mcp.json         MCP config, when the agent plugin writes one (claude-code: always)
        verifier/reward.txt    written by tests/test.sh via $PRIMASE_VERIFIER_DIR
        state/                 scratch dir for fixture servers ($PRIMASE_STATE_DIR)

    $TMPDIR/primase-ws-XXXX/   the agent's cwd (seeded from environment/workspace/), created outside the
                               trial dir and any repo so the agent cannot walk up into the job dir or the
                               task's tests; left on disk after the trial.

The agent process does not get $PRIMASE_TASK_DIR / _TRIAL_DIR / _AGENT_DIR / _VERIFIER_DIR / _STATE_DIR
(only $PRIMASE_PORT and $PRIMASE_WORKSPACE); local-setup.sh and tests/test.sh get all of them.

Task conventions (all optional except tests/test.sh):
    environment/workspace/       copied into the agent's cwd (starter files); nothing else is copied.
    environment/local-setup.sh   run before the agent (cwd=workspace); may start background servers.
                                 Its whole process group is killed after the verifier.
    {{PRIMASE_PORT}}             replaced in instruction.md with a free TCP port (also $PRIMASE_PORT).
    {{env:NAME}}                 replaced with $NAME from the runner env (e.g. a self-hosted site URL).
    tests/test.sh                run after the agent with cwd=workspace; writes reward to
                                 $PRIMASE_VERIFIER_DIR/reward.txt (falls back to exit code 0 -> 1.0).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .agents import LocalInvocation, get_agent


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


#: runner paths the agent must not be handed (they lead to the task's tests / answers and the verifier dir)
_HIDDEN_FROM_AGENT = frozenset(
    {"PRIMASE_TASK_DIR", "PRIMASE_TRIAL_DIR", "PRIMASE_AGENT_DIR", "PRIMASE_VERIFIER_DIR", "PRIMASE_STATE_DIR"}
)

_ENV_PLACEHOLDER = re.compile(r"\{\{env:([A-Za-z_][A-Za-z0-9_]*)\}\}")


def render_instruction(text: str, port: int, env: dict[str, str]) -> str:
    """``{{PRIMASE_PORT}}`` → free port; ``{{env:NAME}}`` → value of NAME (KeyError if unset)."""
    text = text.replace("{{PRIMASE_PORT}}", str(port))

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in env:
            raise KeyError(f"instruction placeholder {{{{env:{name}}}}} but ${name} is not set")
        return env[name]

    return _ENV_PLACEHOLDER.sub(sub, text)


def _kill_group(proc: subprocess.Popen[Any] | None) -> None:
    """SIGTERM the process group, wait up to 5 s for it to disappear, then SIGKILL what is left."""
    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        proc.poll()  # reap the leader so a zombie does not keep the group alive
        try:
            os.killpg(proc.pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.1)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _span(start: str, end: str) -> dict[str, str]:
    return {"started_at": start, "finished_at": end}


def _prepare_workspace(task_dir: Path, ws: Path) -> None:
    """Seed the agent's cwd from ``environment/workspace/`` only.

    Everything else under ``environment/`` (fixture sites, servers, Docker files) and ``tests/`` stays
    out of the agent's working directory, and the agent's env carries no path to the task or trial dir.
    This is not a sandbox: the agent runs as the same user and could still find those files by
    searching the disk. ``local-setup.sh`` reaches them via ``$PRIMASE_TASK_DIR``.
    """
    src = task_dir / "environment" / "workspace"
    if src.is_dir():
        shutil.copytree(src, ws, dirs_exist_ok=True)


def _read_rewards(verifier_dir: Path, rc: int | None) -> dict[str, Any]:
    if (verifier_dir / "reward.json").exists():
        data = json.loads((verifier_dir / "reward.json").read_text())
        return data if isinstance(data, dict) else {"reward": float(data)}
    if (verifier_dir / "reward.txt").exists():
        return {"reward": float((verifier_dir / "reward.txt").read_text().strip() or 0)}
    if rc is not None:
        return {"reward": 1.0 if rc == 0 else 0.0}
    return {}


def run(spec: dict[str, Any]) -> dict[str, Any]:
    task_dir = Path(spec["task_dir"])
    trial = Path(spec["trial_dir"])
    if trial.exists():
        shutil.rmtree(trial)
    agent_dir, verifier_dir, state_dir = trial / "agent", trial / "verifier", trial / "state"
    for d in (agent_dir, verifier_dir, state_dir):
        d.mkdir(parents=True)
    ws = Path(tempfile.mkdtemp(prefix="primase-ws-"))
    toml = tomllib.loads((task_dir / "task.toml").read_text())
    agent_timeout = float((toml.get("agent") or {}).get("timeout_sec") or 600) * float(
        spec.get("agent_timeout_multiplier") or 1
    )
    verifier_timeout = float((toml.get("verifier") or {}).get("timeout_sec") or 300)
    _prepare_workspace(task_dir, ws)

    port = free_port()
    env = dict(os.environ)
    env.update(
        {
            "PRIMASE_PORT": str(port),
            "PRIMASE_TASK_DIR": str(task_dir),
            "PRIMASE_TRIAL_DIR": str(trial),
            "PRIMASE_WORKSPACE": str(ws),
            "PRIMASE_AGENT_DIR": str(agent_dir),
            "PRIMASE_VERIFIER_DIR": str(verifier_dir),
            "PRIMASE_STATE_DIR": str(state_dir),
        }
    )
    agent_env = {k: v for k, v in env.items() if k not in _HIDDEN_FROM_AGENT}

    result: dict[str, Any] = {
        "task_name": spec["task"],
        "trial_name": trial.name,
        "workspace": str(ws),
        "started_at": _now(),
        "agent_info": {"name": spec["agent"], "version": None},
        "config": {"agent": {"name": spec["agent"], "model_name": spec.get("model")}, "backend": "local"},
    }
    setup_proc: subprocess.Popen[Any] | None = None
    try:
        instruction = render_instruction((task_dir / "instruction.md").read_text(), port, env)
        (agent_dir / "instruction.md").write_text(instruction)
        setup = task_dir / "environment" / "local-setup.sh"
        t0 = _now()
        if setup.exists():
            setup_proc = subprocess.Popen(["bash", str(setup)], cwd=ws, env=env, start_new_session=True)
            rc = setup_proc.wait(timeout=300)
            if rc != 0:
                raise RuntimeError(f"local-setup.sh exited {rc}")
        result["environment_setup"] = _span(t0, _now())

        inv = LocalInvocation(
            instruction=instruction,
            model=spec.get("model"),
            workspace=ws,
            agent_dir=agent_dir,
            kwargs=dict(spec.get("agent_kwargs") or {}),
            mcp_servers=list(spec.get("mcp_servers") or []),
        )
        cmd = get_agent(spec["agent"]).local_command(inv)
        if cmd is None:
            raise RuntimeError(f"agent {spec['agent']!r} does not support the local backend")
        for path, content in cmd.files.items():
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(content)
        shown = ["<instruction>" if a == instruction else a for a in cmd.argv]
        (agent_dir / "command.json").write_text(json.dumps(shown))
        a0 = _now()
        with (agent_dir / cmd.stdout_file).open("w") as out, (agent_dir / "stderr.txt").open("w") as err:
            proc = subprocess.Popen(
                cmd.argv,
                cwd=ws,
                env={**agent_env, **cmd.env},
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                proc.wait(timeout=agent_timeout)
            except subprocess.TimeoutExpired:
                result["exception_info"] = {
                    "exception_type": "AgentTimeoutError",
                    "exception_message": f"agent exceeded {agent_timeout:.0f}s",
                }
            _kill_group(proc)  # also on a normal exit: children the agent left behind must not outlive the trial
            proc.wait()
        result["agent_execution"] = _span(a0, _now())
        result["agent_result"] = {"exit_code": proc.returncode}

        v0 = _now()
        test = task_dir / "tests" / "test.sh"
        rc_v: int | None
        try:
            vr = subprocess.run(
                ["bash", str(test)],
                cwd=ws,
                env={**env, "PRIMASE_TESTS_DIR": str(test.parent)},
                capture_output=True,
                text=True,
                timeout=verifier_timeout,
            )
            (verifier_dir / "test-stdout.txt").write_text(vr.stdout + vr.stderr)
            rc_v = vr.returncode
        except subprocess.TimeoutExpired:
            rc_v = None
            result.setdefault(
                "exception_info", {"exception_type": "VerifierTimeoutError", "exception_message": "verifier timeout"}
            )
        result["verifier"] = _span(v0, _now())
        result["verifier_result"] = {"rewards": _read_rewards(verifier_dir, rc_v)}
    except Exception as e:  # noqa: BLE001
        result["exception_info"] = {"exception_type": type(e).__name__, "exception_message": str(e)[:500]}
    finally:
        _kill_group(setup_proc)
        result["finished_at"] = _now()
        (trial / "result.json").write_text(json.dumps(result, indent=2))
    return result


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m primase.local_runner <spec.json>", file=sys.stderr)
        return 2
    res = run(json.loads(Path(args[0]).read_text()))
    rewards = (res.get("verifier_result") or {}).get("rewards") or {}
    print(
        json.dumps({"task": res["task_name"], "reward": rewards.get("reward"), "exception": res.get("exception_info")})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
