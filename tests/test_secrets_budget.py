import os

import pytest

from primase.budget import BudgetExceeded, BudgetGuard
from primase.config import BudgetSpec
from primase.schema import TrialResult
from primase.secrets import REDACTED, SecretStore, parse_env_file

FAKE = "fake_test_secret_0123456789abcdef"


def test_env_file_parse_and_perms(tmp_path):
    f = tmp_path / "e.env"
    f.write_text(f"# c\nexport CURSOR_API_KEY='{FAKE}'\nOTHER=x y # c\nBAD LINE\n")
    os.chmod(f, 0o600)
    assert parse_env_file(f)["CURSOR_API_KEY"] == FAKE
    s = SecretStore.load(["CURSOR_API_KEY", "NOPE"], f, environ={})
    assert s.missing() == ["NOPE"] and s.env() == {"CURSOR_API_KEY": FAKE}
    assert FAKE not in repr(s)
    os.chmod(f, 0o644)
    with pytest.raises(PermissionError):
        SecretStore.load(["CURSOR_API_KEY"], f, environ={})


def test_redact_and_scrub(tmp_path):
    s = SecretStore.load(["K"], None, environ={"K": FAKE})
    assert s.redactor(f"x {FAKE} y") == f"x {REDACTED} y"
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "cursor-cli.txt").write_text(f"CURSOR_API_KEY={FAKE}\n")
    (tmp_path / "clean.json").write_text("{}")
    hits = s.redactor.scrub_tree(tmp_path)
    assert [h.name for h in hits] == ["cursor-cli.txt"]
    assert FAKE not in (tmp_path / "agent" / "cursor-cli.txt").read_text()


def _t(**kw):
    return TrialResult(job_name="j", backend="harbor", task="t", status="passed", **kw)


def test_budget_stops_on_cli_reported_cost():
    g = BudgetGuard(BudgetSpec())
    g.observe(_t(cost_usd=1.0, cost_source="cursor_pricing"))  # estimate only: no cap set → fine
    with pytest.raises(BudgetExceeded, match="billed"):
        g.observe(_t(cost_usd=0.01, cost_source="cursor_cli", cli_reported_cost_usd=0.01))


def test_budget_stops_on_claude_reported_cost():
    g = BudgetGuard(BudgetSpec())
    with pytest.raises(BudgetExceeded, match="billed"):
        g.observe(_t(cost_usd=0.12, cost_source="claude_code", cli_reported_cost_usd=0.12))


def test_budget_zero_reported_cost_ok_and_estimate_cap():
    g = BudgetGuard(BudgetSpec(max_estimated_cost_usd=1.0))
    g.observe(_t(cost_source="cursor_cli", cli_reported_cost_usd=0.0))
    g.observe(_t(cost_usd=0.6, cost_source="cursor_pricing"))
    with pytest.raises(BudgetExceeded, match="estimated"):
        g.observe(_t(cost_usd=0.6, cost_source="cursor_pricing"))


def test_included_models():
    b = BudgetSpec(included_models=["composer-2.5", "grok-4.7-high"])
    assert b.model_allowed("grok-4.7-high") and not b.model_allowed("claude-opus-5-high")
