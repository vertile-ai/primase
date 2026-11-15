"""Budget guard. Core policy is generic (billed CLI cost + estimate caps).

Product-specific allow-lists (e.g. Cursor ``included_models``) are supplied as
:class:`~primase.agents.base.BudgetHook` instances from the agent plugin.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .agents.base import BudgetHook
from .config import BudgetSpec
from .schema import TrialResult


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class BudgetGuard:
    spec: BudgetSpec
    hooks: list[BudgetHook] = field(default_factory=list)
    billed_sources: frozenset[str] | None = None
    reported_total: float = 0.0
    estimated_total: float = 0.0
    events: list[str] = field(default_factory=list)

    def _sources(self) -> frozenset[str]:
        if self.billed_sources is not None:
            return self.billed_sources
        from .agents import billed_cost_sources

        return billed_cost_sources()

    def check_model(self, model_id: str) -> None:
        for hook in self.hooks:
            hook.check_model(model_id)

    def observe(self, r: TrialResult) -> None:
        """Account one finished trial; raises BudgetExceeded when a cap is crossed."""
        billed = r.cli_reported_cost_usd is not None or (r.cost_source in self._sources())
        if billed:
            c = float(r.cli_reported_cost_usd if r.cli_reported_cost_usd is not None else (r.cost_usd or 0.0))
            self.reported_total += c
            for hook in self.hooks:
                hook.observe_billed(r.job_name, c)
            if self.spec.stop_on_billed_cost and c > 0:
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
