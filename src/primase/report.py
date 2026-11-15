"""Reports: per-trial CSV + markdown summary.

Rules:
  * pass rate per (suite, model, variant); a non-baseline variant with a LOWER pass rate
    than the baseline is a regression.
  * token/time deltas only over (suite, task, model) where BOTH variants have >=1 passing
    trial; per variant the median over passing trials, then delta% = (V - baseline) / baseline;
    the summary shows the median of per-task deltas.
  * N-arm: every non-baseline variant is paired against the baseline independently.
  * Optional bootstrap CI on the median of per-task delta% (cheap, deterministic seed).
"""

from __future__ import annotations

import csv
import random
import statistics
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .schema import CSV_FIELDS, TrialResult

METRICS = (
    "total_tokens",
    "prompt_tokens_total",
    "output_tokens",
    "cache_read_tokens",
    "tool_output_tokens_est",
    "shell_output_tokens_est",
    "n_turns",
    "n_tool_calls",
    "agent_s",
    "duration_s",
    "cost_usd",
)


def write_csv(rows: Iterable[TrialResult], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r.model_dump(mode="json"))
    return path


def _median(xs: list[Any]) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def bootstrap_ci(
    values: list[float],
    *,
    n_boot: int = 1000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float | None, float | None, float | None]:
    """Return (median, lo, hi) of a percentile bootstrap CI on the median. Cheap & deterministic."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None, None, None
    med = statistics.median(vals)
    if len(vals) == 1:
        return med, med, med
    rng = random.Random(seed)
    samples: list[float] = []
    n = len(vals)
    for _ in range(n_boot):
        draw = [vals[rng.randrange(n)] for _ in range(n)]
        samples.append(statistics.median(draw))
    samples.sort()
    lo_i = int(alpha / 2 * n_boot)
    hi_i = int((1 - alpha / 2) * n_boot) - 1
    hi_i = max(0, min(hi_i, n_boot - 1))
    return med, samples[lo_i], samples[hi_i]


def pass_rates(rows: list[TrialResult]) -> dict[tuple[str, str, str], tuple[int, int]]:
    agg: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        k = (r.suite or "?", r.model or "?", r.variant or "?")
        agg[k][1] += 1
        agg[k][0] += int(r.passed)
    return {k: (v[0], v[1]) for k, v in agg.items()}


def regressions(rows: list[TrialResult], baseline: str = "control") -> list[dict[str, Any]]:
    pr = pass_rates(rows)
    out: list[dict[str, Any]] = []
    for (suite, model, variant), (p, n) in pr.items():
        if variant == baseline or (suite, model, baseline) not in pr:
            continue
        bp, bn = pr[(suite, model, baseline)]
        ra, rb = bp / bn if bn else 0.0, p / n if n else 0.0
        out.append(
            {
                "suite": suite,
                "model": model,
                "variant": variant,
                "baseline_rate": ra,
                "variant_rate": rb,
                "regression": rb < ra,
            }
        )
    return out


def paired_deltas(rows: list[TrialResult], baseline: str = "control") -> list[dict[str, Any]]:
    by: dict[tuple[str, str, str], dict[str, list[TrialResult]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by[(r.suite or "?", r.task, r.model or "?")][r.variant or "?"].append(r)
    out: list[dict[str, Any]] = []
    for (suite, task, model), variants in sorted(by.items()):
        base = [r for r in variants.get(baseline, []) if r.passed]
        for variant, rs in variants.items():
            if variant == baseline:
                continue
            cand = [r for r in rs if r.passed]
            if not base or not cand:
                continue
            # rows without usage (agent timed out before its final usage event) cannot be compared on tokens
            base_u = [r for r in base if not r.usage_missing] or base
            cand_u = [r for r in cand if not r.usage_missing] or cand
            d: dict[str, Any] = {
                "suite": suite,
                "task": task,
                "model": model,
                "variant": variant,
                "n_base": len(base_u),
                "n_variant": len(cand_u),
            }
            for m in METRICS:
                a = _median([getattr(r, m) for r in base_u])
                b = _median([getattr(r, m) for r in cand_u])
                d[f"{m}_base"], d[f"{m}_variant"] = a, b
                if a is None or b is None or a == 0:
                    d[f"{m}_delta_pct"] = None
                else:
                    d[f"{m}_delta_pct"] = 100.0 * (b - a) / a
            out.append(d)
    return out


def summary_delta_cis(
    deltas: list[dict[str, Any]],
    metric: str = "total_tokens_delta_pct",
) -> list[dict[str, Any]]:
    """Per (model, variant) median-of-task-deltas with bootstrap CI."""
    grp: dict[tuple[str, str], list[float]] = defaultdict(list)
    for d in deltas:
        v = d.get(metric)
        if v is not None:
            grp[(d["model"], d["variant"])].append(float(v))
    out: list[dict[str, Any]] = []
    for (model, variant), vals in sorted(grp.items()):
        med, lo, hi = bootstrap_ci(vals, seed=hash((model, variant, metric)) & 0xFFFFFFFF)
        out.append(
            {
                "model": model,
                "variant": variant,
                "metric": metric,
                "n_tasks": len(vals),
                "median": med,
                "ci_lo": lo,
                "ci_hi": hi,
            }
        )
    return out


def _fmt(v: Any, pct: bool = False) -> str:
    if v is None:
        return "–"
    if pct:
        return f"{v:+.1f}%"
    if isinstance(v, float):
        return f"{v:,.4f}" if abs(v) < 10 else f"{v:,.1f}"
    return f"{v:,}"


def markdown(rows: list[TrialResult], title: str, baseline: str = "control", notes: list[str] | None = None) -> str:
    lines = [f"# {title}", ""]
    if notes:
        lines += [f"> {n}" for n in notes] + [""]
    variants = sorted({r.variant or "?" for r in rows})
    lines += [
        f"Baseline: `{baseline}` · variants: {', '.join(f'`{v}`' for v in variants)}",
        "",
        "## Pass rate",
        "",
        "| suite | model | variant | agent | passed / trials | rate |",
        "|---|---|---|---|---|---|",
    ]
    # agent lookup per (suite, model, variant)
    agent_of: dict[tuple[str, str, str], str] = {}
    for r in rows:
        agent_of[(r.suite or "?", r.model or "?", r.variant or "?")] = r.agent or "–"
    for (s, m, v), (p, n) in sorted(pass_rates(rows).items()):
        agent = agent_of.get((s, m, v), "–")
        if n:
            lines.append(f"| {s} | {m} | {v} | {agent} | {p} / {n} | {p / n:.0%} |")
        else:
            lines.append(f"| {s} | {m} | {v} | {agent} | 0 / 0 | – |")
    regs = regressions(rows, baseline)
    if regs:
        lines += [
            "",
            "## Regression check (variant pass rate < baseline → REGRESSION)",
            "",
            "| suite | model | variant | baseline | variant | verdict |",
            "|---|---|---|---|---|---|",
        ]
        for reg in regs:
            lines.append(
                f"| {reg['suite']} | {reg['model']} | {reg['variant']} | {reg['baseline_rate']:.0%} | "
                f"{reg['variant_rate']:.0%} | {'**REGRESSION**' if reg['regression'] else 'ok'} |"
            )
    deltas = paired_deltas(rows, baseline)
    lines += ["", "## Deltas over tasks both variants passed (medians over passing trials)", ""]
    if not deltas:
        lines.append("_No task passed in both variants yet – deltas withheld (they would reward failing faster)._")
    else:
        lines += [
            "| suite | task | model | variant | tokens | tool-output tok | shell-output tok | turns | agent s | cost |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for d in deltas:
            lines.append(
                f"| {d['suite']} | {d['task']} | {d['model']} | {d['variant']} | "
                f"{_fmt(d['total_tokens_delta_pct'], True)} | "
                f"{_fmt(d['tool_output_tokens_est_delta_pct'], True)} | "
                f"{_fmt(d['shell_output_tokens_est_delta_pct'], True)} | "
                f"{_fmt(d['n_turns_delta_pct'], True)} | "
                f"{_fmt(d['agent_s_delta_pct'], True)} | {_fmt(d['cost_usd_delta_pct'], True)} |"
            )
        lines += [
            "",
            "| model | variant | tasks | median Δ tokens | 95% CI | median Δ agent time | 95% CI |",
            "|---|---|---|---|---|---|---|",
        ]
        tok_cis = {(c["model"], c["variant"]): c for c in summary_delta_cis(deltas, "total_tokens_delta_pct")}
        time_cis = {(c["model"], c["variant"]): c for c in summary_delta_cis(deltas, "agent_s_delta_pct")}
        keys = sorted(set(tok_cis) | set(time_cis))
        for key in keys:
            tc, ac = tok_cis.get(key), time_cis.get(key)
            n = (tc or ac or {}).get("n_tasks", 0)
            lines.append(
                f"| {key[0]} | {key[1]} | {n} | {_fmt(tc['median'] if tc else None, True)} | "
                f"[{_fmt(tc['ci_lo'] if tc else None, True)}, {_fmt(tc['ci_hi'] if tc else None, True)}] | "
                f"{_fmt(ac['median'] if ac else None, True)} | "
                f"[{_fmt(ac['ci_lo'] if ac else None, True)}, {_fmt(ac['ci_hi'] if ac else None, True)}] |"
            )
    lines += [
        "",
        "## Trials",
        "",
        (  # noqa: E501
            "| suite | task | model | variant | agent | # | status | reward | in | out | "
            "cache r/w | cost (source) | turns | tools | tool-out chars | "
            "shell-out chars | agent s | total s |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in sorted(rows, key=lambda r: (r.suite or "", r.task, r.model or "", r.variant or "", r.trial_index or 0)):
        lines.append(
            f"| {row.suite} | {row.task} | {row.model} | {row.variant} | {row.agent or '–'} | {row.trial_index} | "
            f"{row.status}{' (timeout)' if row.agent_timed_out else ''}{' (no usage)' if row.usage_missing else ''} | "
            f"{_fmt(row.reward)} | {_fmt(row.input_tokens)} | {_fmt(row.output_tokens)} | "
            f"{_fmt(row.cache_read_tokens)}/{_fmt(row.cache_write_tokens)} | "
            f"{_fmt(row.cost_usd)} ({row.cost_source or '–'}) | {_fmt(row.n_turns)} | {_fmt(row.n_tool_calls)} | "
            f"{_fmt(row.tool_output_chars)} | {_fmt(row.shell_output_chars)} | "
            f"{_fmt(row.agent_s)} | {_fmt(row.duration_s)} |"
        )
    errs = [r for r in rows if r.exception]
    if errs:
        lines += ["", "## Exceptions", ""] + [f"- `{r.job_name}`: {r.exception}" for r in errs]
    lines += [
        "",
        "_cost source: agent-plugin name (e.g. `cursor_cli` / `claude_code` / `codex`) = reported by the CLI "
        "(treated as billed); `cursor_pricing` / `litellm` = harness price-table estimate, not a bill. "
        "tool-output tokens ≈ chars/4. Bootstrap CIs use 1000 resamples of per-task median deltas._",
        "",
    ]
    return "\n".join(lines)
