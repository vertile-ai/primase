"""Agent plugin registry + noop collector."""

import json
from pathlib import Path

from primase.agents import (
    AgentMetrics,
    AgentPlugin,
    collect_agent_metrics,
    get_agent,
    list_agents,
    register_agent,
    reset_registry,
)
from primase.agents.noop import NoopPlugin
from primase.collect import collect_trial


def test_builtin_agents_registered():
    reset_registry()
    names = list_agents()
    assert "cursor-cli" in names and "claude-code" in names and "codex" in names and "noop" in names
    assert get_agent("cursor-cli").name == "cursor-cli"


def test_register_custom_agent(tmp_path):
    reset_registry()

    class EchoPlugin(AgentPlugin):
        name = "echo"

        def collect(self, agent_dir: Path) -> AgentMetrics:
            return AgentMetrics(has_usage=True, input_tokens=1, output_tokens=2)

    register_agent(EchoPlugin())
    assert "echo" in list_agents()
    m = get_agent("echo").collect(tmp_path)
    assert m.input_tokens == 1 and m.output_tokens == 2
    reset_registry()
    assert "echo" not in list_agents()


def test_noop_plugin_collect(tmp_path):
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "noop.json").write_text(
        json.dumps(
            {
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "cost_usd": 0.0,
                "n_turns": 1,
                "n_tool_calls": 0,
                "version": "0.0.1",
            }
        )
    )
    m = NoopPlugin().collect(agent)
    assert m.has_usage and m.input_tokens == 10 and m.output_tokens == 5
    assert m.cost_source == "noop" and m.agent_version == "0.0.1"

    # preferred agent path through collect_agent_metrics
    m2 = collect_agent_metrics(agent, preferred="noop")
    assert m2.input_tokens == 10


def test_cursor_kwargs_via_plugin():
    p = get_agent("cursor-cli")
    assert p.harbor_kwargs({"config": "/x.json", "n": 2}) == {"config": "/x.json", "n": "2"}


def test_collect_prefers_named_agent(fixtures, tmp_path):
    # copy cursor fixture layout but ensure preferred agent is honored
    src = fixtures / "jobs/anb-x-A/react-lead-form__aaa"
    r = collect_trial(src, "harbor", preferred_agent="cursor-cli")
    assert r.input_tokens == 500
