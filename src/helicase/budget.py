"""Budget guard. Policy (default): stop the run on ANY cost the agent CLI itself reports
(= billed usage, e.g. on-demand/credit billing), and optionally on a runaway price-table estimate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import BudgetSpec
from .schema import TrialResult

# cost_source values that mean "the agent CLI itself reported a billed cost"
BILLED_COST_SOURCES = frozenset({"cursor_cli", "claude_code", "codex"})


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class BudgetGuard:
    spec: BudgetSpec
    reported_total: float = 0.0
    estimated_total: float = 0.0
    events: list[str] = field(default_factory=list)

    def check_model(self, model_id: str) -> None:
        if not self.spec.model_allowed(model_id):
            raise BudgetExceeded(f"model {model_id} not in included_models {self.spec.included_models}")

    def observe(self, r: TrialResult) -> None:
        """Account one finished trial; raises BudgetExceeded when a cap is crossed."""
        billed = r.cli_reported_cost_usd is not None or (r.cost_source in BILLED_COST_SOURCES)
        if billed:
            c = float(r.cli_reported_cost_usd if r.cli_reported_cost_usd is not None else (r.cost_usd or 0.0))
            self.reported_total += c
            if self.spec.stop_on_credit_billing and c > 0:
                raise BudgetExceeded(
                    f"{r.job_name}: agent CLI reported a billed cost of ${c:.4f} (credit/on-demand billing) – stopping"
                )
            if self.reported_total > self.spec.max_reported_cost_usd:
                raise BudgetExceeded(
                    f"CLI-reported cost ${self.reported_total:.4f} > cap ${self.spec.max_reported_cost_usd:.2f}"
                )
        elif r.cost_usd is not None:
            self.estimated_total += float(r.cost_usd)
            cap = self.spec.max_estimated_cost_usd
            if cap is not None and self.estimated_total > cap:
                raise BudgetExceeded(f"estimated cost ${self.estimated_total:.4f} > cap ${cap:.2f}")
