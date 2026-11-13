"""Reports: per-trial CSV + markdown summary.

Rules:
  * pass rate per (suite, model, variant); a non-baseline variant with a LOWER pass rate
    than the baseline is a regression.
  * token/time deltas only over (suite, task, model) where BOTH variants have >=1 passing
    trial; per variant the median over passing trials, then delta% = (V - baseline) / baseline;
    the summary shows the median of per-task deltas.
"""

from __future__ import annotations

import csv
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
    lines += [
        "## Pass rate",
        "",
        "| suite | model | variant | passed / trials | rate |",
        "|---|---|---|---|---|",
    ]
    for (s, m, v), (p, n) in sorted(pass_rates(rows).items()):
        lines.append(f"| {s} | {m} | {v} | {p} / {n} | {p / n:.0%} |" if n else f"| {s} | {m} | {v} | 0 / 0 | – |")
    regs = regressions(rows, baseline)
    if regs:
        lines += [
            "",
            "## Regression check (variant pass rate < baseline → REGRESSION)",
            "",
            "| suite | model | variant | baseline | variant | verdict |",
            "|---|---|---|---|---|---|",
        ]
        for r in regs:
            lines.append(
                f"| {r['suite']} | {r['model']} | {r['variant']} | {r['baseline_rate']:.0%} | "
                f"{r['variant_rate']:.0%} | {'**REGRESSION**' if r['regression'] else 'ok'} |"
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
            "| model | variant | tasks | median Δ tokens | median Δ agent time |",
            "|---|---|---|---|---|",
        ]
        grp: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for d in deltas:
            grp[(d["model"], d["variant"])].append(d)
        for (m, v), ds in sorted(grp.items()):
            lines.append(
                f"| {m} | {v} | {len(ds)} | {_fmt(_median([d['total_tokens_delta_pct'] for d in ds]), True)} | "
                f"{_fmt(_median([d['agent_s_delta_pct'] for d in ds]), True)} |"
            )
    lines += [
        "",
        "## Trials",
        "",
        (  # noqa: E501
            "| suite | task | model | variant | # | status | reward | in | out | "
            "cache r/w | cost (source) | turns | tools | tool-out chars | "
            "shell-out chars | agent s | total s |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in sorted(rows, key=lambda r: (r.suite or "", r.task, r.model or "", r.variant or "", r.trial_index or 0)):
        lines.append(
            f"| {row.suite} | {row.task} | {row.model} | {row.variant} | {row.trial_index} | "
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
        "_cost source: `cursor_cli` / `claude_code` / `codex` = reported by the CLI (treated as billed); "
        "`cursor_pricing` / `litellm` = harness price-table estimate, not a bill. "
        "tool-output tokens ≈ chars/4._",
        "",
    ]
    return "\n".join(lines)
