"""Reports: per-trial CSV + markdown summary.

Primase's question is always the same, in this order:
  1. correctness gate — did the variant hold the baseline's pass rate?
  2. only then — how many tokens (and how much time) did it save?

Rules:
  * gate per (suite, model, variant) vs the baseline, only over (task, trial_index) cells present in
    BOTH arms with status passed/failed; error/missing rows are counted separately. Per task the
    pass-rate difference (variant − baseline); gate statistic = mean over tasks with a bootstrap CI
    over tasks. ``hold`` (mean >= 0), ``REGRESSION`` (mean < 0 and CI upper < 0), else ``unclear``
    (also when either arm's error share exceeds MAX_ERROR_RATE or no common cells exist).
  * token/time deltas only over (suite, task, model) where BOTH arms have >=1 token-usable trial
    (passed, usage complete, agent not timed out); per arm the median over those trials, then
    delta% = (V - baseline) / baseline; the summary shows the median of per-task deltas with a
    two-level bootstrap CI (tasks, then trials within task), plus the aggregate ratio.
  * a directional token verdict needs gate ``hold`` and >= MIN_PAIRED_TASKS paired tasks.
  * N-arm: every non-baseline variant is paired against the baseline independently.
  * Bootstrap seeds are derived from a stable digest of the group key (deterministic across runs).
"""

from __future__ import annotations

import csv
import json
import random
import statistics
import zlib
from collections import defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .schema import CSV_FIELDS, TrialResult

METRICS = (
    "total_tokens",
    "prompt_tokens_total",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "tool_output_tokens_est",
    "shell_output_tokens_est",
    "n_turns",
    "n_tool_calls",
    "agent_s",
    "duration_s",
    "cost_usd",
)

VERDICT_METRICS = ("total_tokens", "prompt_tokens_total", "output_tokens", "n_turns", "agent_s", "cost_usd")

MIN_PAIRED_TASKS = 6  # sign-test floor: fewer tasks cannot reach p < 0.05
MAX_ERROR_RATE = 0.10  # above this error share in either arm the gate is withheld
GATED_STATUSES = ("passed", "failed")


def write_csv(rows: Iterable[TrialResult], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            row = r.model_dump(mode="json")
            w.writerow({k: json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v for k, v in row.items()})
    return path


def row_from_csv(rec: dict[str, str]) -> TrialResult:
    """Inverse of :func:`write_csv`: empty → None, JSON-encoded dict/list columns decoded."""
    clean: dict[str, Any] = {}
    for k, v in rec.items():
        if v == "" or v is None:
            clean[k] = None
        elif v[:1] in "{[":
            try:
                clean[k] = json.loads(v)
            except ValueError:
                clean[k] = v
        else:
            clean[k] = v
    return TrialResult.model_validate(clean)


def _median(xs: list[Any]) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _seed(*parts: Any) -> int:
    """Stable bootstrap seed (independent of PYTHONHASHSEED)."""
    return zlib.crc32("|".join(str(p) for p in parts).encode())


def _usable(r: TrialResult) -> bool:
    """A row whose token/time numbers are trustworthy enough to enter deltas and medians."""
    return r.passed and not r.usage_missing and not r.usage_partial and not r.agent_timed_out


def _pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or a == 0:
        return None
    return 100.0 * (b - a) / a


def _bounds(samples: list[float], alpha: float) -> tuple[float | None, float | None]:
    if not samples:
        return None, None
    samples = sorted(samples)
    n = len(samples)
    lo_i = int(alpha / 2 * n)
    hi_i = max(0, min(int((1 - alpha / 2) * n) - 1, n - 1))
    return samples[lo_i], samples[hi_i]


def bootstrap_ci(
    values: list[float],
    *,
    n_boot: int = 1000,
    alpha: float = 0.05,
    seed: int = 0,
    stat: Callable[[list[float]], float] = statistics.median,
) -> tuple[float | None, float | None, float | None]:
    """Return (stat, lo, hi) of a percentile bootstrap CI on ``stat`` (median by default)."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None, None, None
    est = stat(vals)
    if len(vals) == 1:
        return est, est, est
    rng = random.Random(seed)
    n = len(vals)
    samples = [stat([vals[rng.randrange(n)] for _ in range(n)]) for _ in range(n_boot)]
    lo, hi = _bounds(samples, alpha)
    return est, lo, hi


def _two_level_ci(
    pairs: list[tuple[list[float], list[float]]],
    *,
    n_boot: int = 1000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float | None, float | None, float | None]:
    """Median per-task delta% with a two-level bootstrap CI.

    ``pairs`` holds, per task, the (baseline, variant) trial values. Each resample draws tasks with
    replacement and, within each drawn task, each arm's trials with replacement, then recomputes the
    per-arm medians, the task delta% and the median over tasks.
    """
    pairs = [(a, b) for a, b in pairs if a and b]
    obs = [d for d in (_pct(statistics.median(a), statistics.median(b)) for a, b in pairs) if d is not None]
    if not obs:
        return None, None, None
    med = statistics.median(obs)
    rng = random.Random(seed)
    n = len(pairs)
    samples: list[float] = []
    for _ in range(n_boot):
        ds: list[float] = []
        for _ in range(n):
            a, b = pairs[rng.randrange(n)]
            d = _pct(
                statistics.median([a[rng.randrange(len(a))] for _ in a]),
                statistics.median([b[rng.randrange(len(b))] for _ in b]),
            )
            if d is not None:
                ds.append(d)
        if ds:
            samples.append(statistics.median(ds))
    lo, hi = _bounds(samples, alpha)
    return med, lo, hi


def _trial_pairs(deltas: list[dict[str, Any]], metric: str) -> list[tuple[list[float], list[float]]]:
    return [
        (
            [float(v) for r in d["base_trials"] if (v := getattr(r, metric)) is not None],
            [float(v) for r in d["variant_trials"] if (v := getattr(r, metric)) is not None],
        )
        for d in deltas
    ]


def _arms(rows: list[TrialResult]) -> dict[tuple[str, str, str], list[TrialResult]]:
    arms: dict[tuple[str, str, str], list[TrialResult]] = defaultdict(list)
    for r in rows:
        arms[(r.suite or "?", r.model or "?", r.variant or "?")].append(r)
    return arms


def pass_rates(rows: list[TrialResult]) -> dict[tuple[str, str, str], tuple[int, int]]:
    agg: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        k = (r.suite or "?", r.model or "?", r.variant or "?")
        agg[k][1] += 1
        agg[k][0] += int(r.passed)
    return {k: (v[0], v[1]) for k, v in agg.items()}


def _cell_key(c: tuple[str, int | None]) -> tuple[str, int]:
    return c[0], -1 if c[1] is None else c[1]


def _gate(base: list[TrialResult], cand: list[TrialResult], seed: int) -> dict[str, Any]:
    """Paired correctness gate over (task, trial_index) cells gated (passed/failed) in both arms."""
    bc = {(r.task, r.trial_index): r for r in base if r.status in GATED_STATUSES}
    vc = {(r.task, r.trial_index): r for r in cand if r.status in GATED_STATUSES}
    common = sorted(set(bc) & set(vc), key=_cell_key)
    errors_base = sum(r.status not in GATED_STATUSES for r in base)
    errors_variant = sum(r.status not in GATED_STATUSES for r in cand)
    per_task: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])  # base passes, variant passes, cells
    for c in common:
        t = per_task[c[0]]
        t[0] += int(bc[c].passed)
        t[1] += int(vc[c].passed)
        t[2] += 1
    tasks = sorted(per_task)
    diffs = [(per_task[t][1] - per_task[t][0]) / per_task[t][2] for t in tasks]
    diff, lo, hi = bootstrap_ci(diffs, seed=seed, stat=statistics.fmean)
    # rates are fractions of small integers; round away float noise before comparing with 0
    diff, lo, hi = (None if x is None else round(x, 12) for x in (diff, lo, hi))
    n = len(common)
    err_b = errors_base / (errors_base + n) if errors_base + n else 0.0
    err_v = errors_variant / (errors_variant + n) if errors_variant + n else 0.0
    reason = ""
    if not n or diff is None or hi is None:
        gate, reason = "unclear", "no (task, trial) cells gated in both arms"
    elif err_b > MAX_ERROR_RATE or err_v > MAX_ERROR_RATE:
        gate = "unclear"
        reason = f"error rate base {err_b:.0%} / variant {err_v:.0%} exceeds {MAX_ERROR_RATE:.0%}"
    elif diff >= 0:
        gate = "hold"
    elif hi < 0:
        gate = "REGRESSION"
    else:
        gate, reason = "unclear", "pass-rate drop within noise (CI includes 0)"
    return {
        "baseline_rate": sum(bc[c].passed for c in common) / n if n else None,
        "variant_rate": sum(vc[c].passed for c in common) / n if n else None,
        "gate": gate,
        "gate_reason": reason,
        "regression": gate == "REGRESSION",
        "gate_diff": diff,
        "gate_ci_lo": lo,
        "gate_ci_hi": hi,
        "n_gate_tasks": len(tasks),
        "n_gate_cells": n,
        "errors_base": errors_base,
        "errors_variant": errors_variant,
        "lost_tasks": [t for t in tasks if per_task[t][0] > per_task[t][1]],
        "unstable_baseline_tasks": [t for t in tasks if 0 < per_task[t][0] < per_task[t][2]],
    }


def regressions(rows: list[TrialResult], baseline: str = "control") -> list[dict[str, Any]]:
    """Correctness gate per (suite, model, variant); ``regression`` is True only for ``REGRESSION``.

    A variant with rows under a (suite, model) where the baseline has none gets ``gate='no baseline'``.
    """
    arms = _arms(rows)
    out: list[dict[str, Any]] = []
    for (suite, model, variant), rs in sorted(arms.items()):
        if variant == baseline:
            continue
        row: dict[str, Any] = {"suite": suite, "model": model, "variant": variant}
        base = arms.get((suite, model, baseline))
        if not base:
            row.update(
                {
                    "baseline_rate": None,
                    "variant_rate": sum(r.passed for r in rs) / len(rs),
                    "gate": "no baseline",
                    "gate_reason": f"no baseline rows for model {model}",
                    "regression": False,
                    "gate_diff": None,
                    "gate_ci_lo": None,
                    "gate_ci_hi": None,
                    "n_gate_tasks": 0,
                    "n_gate_cells": 0,
                    "errors_base": 0,
                    "errors_variant": sum(r.status not in GATED_STATUSES for r in rs),
                    "lost_tasks": [],
                    "unstable_baseline_tasks": [],
                }
            )
        else:
            row.update(_gate(base, rs, _seed(suite, model, variant, "gate")))
        out.append(row)
    return out


def paired_deltas(rows: list[TrialResult], baseline: str = "control") -> list[dict[str, Any]]:
    """Per (suite, task, model, variant) deltas over token-usable trials; a task needs usable rows in both arms."""
    by: dict[tuple[str, str, str], dict[str, list[TrialResult]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by[(r.suite or "?", r.task, r.model or "?")][r.variant or "?"].append(r)
    out: list[dict[str, Any]] = []
    for (suite, task, model), variants in sorted(by.items()):
        base_u = [r for r in variants.get(baseline, []) if _usable(r)]
        for variant, rs in sorted(variants.items()):
            if variant == baseline:
                continue
            cand_u = [r for r in rs if _usable(r)]
            if not base_u or not cand_u:
                continue
            d: dict[str, Any] = {
                "suite": suite,
                "task": task,
                "model": model,
                "variant": variant,
                "n_base": len(base_u),
                "n_variant": len(cand_u),
                "base_trials": base_u,
                "variant_trials": cand_u,
            }
            for m in METRICS:
                a = _median([getattr(r, m) for r in base_u])
                b = _median([getattr(r, m) for r in cand_u])
                d[f"{m}_base"], d[f"{m}_variant"] = a, b
                d[f"{m}_delta_pct"] = _pct(a, b)
            out.append(d)
    return out


def summary_delta_cis(
    deltas: list[dict[str, Any]],
    metric: str = "total_tokens_delta_pct",
) -> list[dict[str, Any]]:
    """Per (model, variant) median-of-task-deltas with a two-level bootstrap CI."""
    base_metric = metric.removesuffix("_delta_pct")
    grp: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for d in deltas:
        if d.get(metric) is not None:
            grp[(d["model"], d["variant"])].append(d)
    out: list[dict[str, Any]] = []
    for (model, variant), ds in sorted(grp.items()):
        med, lo, hi = _two_level_ci(_trial_pairs(ds, base_metric), seed=_seed(model, variant, metric))
        out.append(
            {
                "model": model,
                "variant": variant,
                "metric": metric,
                "n_tasks": len(ds),
                "median": med,
                "ci_lo": lo,
                "ci_hi": hi,
            }
        )
    return out


def _agents(rs: list[TrialResult]) -> str:
    return ", ".join(sorted({r.agent or "–" for r in rs}))


def verdicts(rows: list[TrialResult], baseline: str = "control") -> list[dict[str, Any]]:
    """Headline per (suite, model, variant): correctness gate first, then the token delta.

    Verdict strings: ``REGRESSION``, ``gate unclear``, ``no baseline rows for model <m>`` (token
    delta never credited); with gate ``hold``: ``no paired tasks``,
    ``inconclusive (n=<k> paired tasks, need 6)``, ``saves tokens`` (CI entirely < 0),
    ``costs tokens`` (CI entirely > 0) or ``inconclusive``.
    """
    deltas = paired_deltas(rows, baseline)
    arms = _arms(rows)
    out: list[dict[str, Any]] = []
    for reg in regressions(rows, baseline):
        key = (reg["suite"], reg["model"], reg["variant"])
        mine = [d for d in deltas if (d["suite"], d["model"], d["variant"]) == key]
        base = arms.get((reg["suite"], reg["model"], baseline), [])
        cand = arms[key]
        row: dict[str, Any] = dict(reg)
        row["n_paired_tasks"] = len(mine)
        row["agent_base"] = _agents(base) if base else None
        row["agent_variant"] = _agents(cand)
        row["excluded_usage_base"] = sum(r.passed and not _usable(r) for r in base)
        row["excluded_usage_variant"] = sum(r.passed and not _usable(r) for r in cand)
        base_ok = {r.task for r in base if _usable(r)}
        cand_ok = {r.task for r in cand if _usable(r)}
        all_tasks = {r.task for r in base} | {r.task for r in cand}
        row["dropped_tasks_base"] = len(cand_ok - base_ok)
        row["dropped_tasks_variant"] = len(base_ok - cand_ok)
        row["dropped_tasks_both"] = len(all_tasks - base_ok - cand_ok)
        for metric in VERDICT_METRICS:
            med, lo, hi = _two_level_ci(_trial_pairs(mine, metric), seed=_seed(*key, metric))
            row[f"{metric}_median"], row[f"{metric}_ci_lo"], row[f"{metric}_ci_hi"] = med, lo, hi
        tok = [d for d in mine if d["total_tokens_base"] is not None and d["total_tokens_variant"] is not None]
        sum_b = sum(d["total_tokens_base"] for d in tok)
        sum_v = sum(d["total_tokens_variant"] for d in tok)
        row["total_tokens_aggregate_delta_pct"] = _pct(sum_b, sum_v) if tok else None
        lo, hi = row["total_tokens_ci_lo"], row["total_tokens_ci_hi"]
        n = row["n_paired_tasks"]
        if reg["gate"] == "no baseline":
            row["verdict"] = reg["gate_reason"]
        elif reg["gate"] == "REGRESSION":
            row["verdict"] = "REGRESSION"
        elif reg["gate"] == "unclear":
            row["verdict"] = "gate unclear"
        elif n == 0:
            row["verdict"] = "no paired tasks"
        elif n < MIN_PAIRED_TASKS:
            row["verdict"] = f"inconclusive (n={n} paired tasks, need {MIN_PAIRED_TASKS})"
        elif lo is not None and hi is not None and hi < 0:
            row["verdict"] = "saves tokens"
        elif lo is not None and hi is not None and lo > 0:
            row["verdict"] = "costs tokens"
        else:
            row["verdict"] = "inconclusive"
        out.append(row)
    return out


ABSOLUTE_METRICS = (
    "total_tokens",
    "prompt_tokens_total",
    "cache_read_tokens",
    "output_tokens",
    "n_turns",
    "n_tool_calls",
    "tool_output_tokens_est",
    "agent_s",
    "cost_usd",
)


def variant_medians(rows: list[TrialResult]) -> list[dict[str, Any]]:
    """Absolute medians per (suite, model, variant) over token-usable trials."""
    out: list[dict[str, Any]] = []
    for (suite, model, variant), rs in sorted(_arms(rows).items()):
        ok = [r for r in rs if _usable(r)]
        d: dict[str, Any] = {"suite": suite, "model": model, "variant": variant, "n_passed": len(ok), "n": len(rs)}
        for m in ABSOLUTE_METRICS:
            d[m] = _median([getattr(r, m) for r in ok])
        out.append(d)
    return out


def tool_usage(rows: list[TrialResult]) -> dict[str, dict[str, int]]:
    """Summed ``tool_calls_by_name`` per variant (all trials)."""
    agg: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        for name, n in (r.tool_calls_by_name or {}).items():
            agg[r.variant or "?"][name] += int(n)
    return {v: dict(sorted(d.items(), key=lambda kv: -kv[1])) for v, d in sorted(agg.items())}


def _fmt(v: Any, pct: bool = False) -> str:
    if v is None:
        return "–"
    if pct:
        return f"{v:+.1f}%"
    if isinstance(v, float):
        return f"{v:,.4f}" if abs(v) < 10 else f"{v:,.1f}"
    return f"{v:,}"


def _rate(v: float | None) -> str:
    return "–" if v is None else f"{v:.0%}"


def markdown(rows: list[TrialResult], title: str, baseline: str = "control", notes: list[str] | None = None) -> str:
    lines = [f"# {title}", ""]
    if notes:
        lines += [f"> {n}" for n in notes] + [""]
    variants = sorted({r.variant or "?" for r in rows})
    lines += [
        f"Baseline: `{baseline}` · variants: {', '.join(f'`{v}`' for v in variants)}",
        "",
    ]
    heads = verdicts(rows, baseline)
    if heads:
        lines += [
            "## Verdict — correctness gate, then token savings",
            "",
            "| suite | model | variant | pass rate (base → variant) | gate | mean Δ pass rate [95% CI] | "
            "errors (base/variant) | paired tasks | dropped tasks (base/variant/both) | "
            "excluded no-usage/timeout (base/variant) | median Δ total tokens [95% CI] | aggregate Δ total tokens | "
            "Δ prompt | Δ output | Δ turns | Δ agent time | Δ cost | verdict |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for h in heads:
            gate = "**REGRESSION**" if h["regression"] else h["gate"]
            if h["gate_reason"] and h["gate"] != "no baseline":
                gate += f" ({h['gate_reason']})"
            gd = h["gate_diff"]
            gate_ci = (
                "–"
                if gd is None
                else f"{100 * gd:+.1f} pp [{100 * h['gate_ci_lo']:+.1f}, {100 * h['gate_ci_hi']:+.1f}]"
            )
            tok = (
                f"{_fmt(h['total_tokens_median'], True)} "
                f"[{_fmt(h['total_tokens_ci_lo'], True)}, {_fmt(h['total_tokens_ci_hi'], True)}]"
            )
            verdict = h["verdict"]
            if h["gate"] == "REGRESSION":
                verdict = "regressed — token delta not credited"
            elif h["gate"] == "unclear":
                verdict += " — token delta not credited"
            lines.append(
                f"| {h['suite']} | {h['model']} | {h['variant']} | "
                f"{_rate(h['baseline_rate'])} → {_rate(h['variant_rate'])} | {gate} | {gate_ci} | "
                f"{h['errors_base']}/{h['errors_variant']} | {h['n_paired_tasks']} | "
                f"{h['dropped_tasks_base']}/{h['dropped_tasks_variant']}/{h['dropped_tasks_both']} | "
                f"{h['excluded_usage_base']}/{h['excluded_usage_variant']} | {tok} | "
                f"{_fmt(h['total_tokens_aggregate_delta_pct'], True)} | "
                f"{_fmt(h['prompt_tokens_total_median'], True)} | "
                f"{_fmt(h['output_tokens_median'], True)} | {_fmt(h['n_turns_median'], True)} | "
                f"{_fmt(h['agent_s_median'], True)} | {_fmt(h['cost_usd_median'], True)} | {verdict} |"
            )
        lines.append("")
        for h in heads:
            label = f"{h['suite']} / {h['model']} / `{h['variant']}`"
            if h["lost_tasks"]:
                lines.append(f"- {label}: lost tasks (baseline pass rate > variant): {', '.join(h['lost_tasks'])}")
            if h["unstable_baseline_tasks"]:
                lines.append(
                    f"- {label}: unstable baseline tasks (0 < baseline pass rate < 1): "
                    f"{', '.join(h['unstable_baseline_tasks'])}"
                )
            if h["agent_base"] is not None and h["agent_base"] != h["agent_variant"]:
                lines.append(
                    f"- {label}: agent `{h['agent_variant']}` vs baseline agent `{h['agent_base']}` — "
                    "token fields are not comparable across agents."
                )
            if h["gate"] == "no baseline":
                lines.append(f"- {label}: {h['gate_reason']} — not compared.")
        if lines[-1] != "":
            lines.append("")
    meds = variant_medians(rows)
    if meds:
        lines += [
            "## Absolute medians per variant (passing trials with complete usage)",
            "",
            "| suite | model | variant | passed | total tok | prompt tok | cache read | output tok | turns | "
            "tool calls | tool-out tok≈ (text) | agent s | cost |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for d in meds:
            lines.append(
                f"| {d['suite']} | {d['model']} | {d['variant']} | {d['n_passed']} / {d['n']} | "
                + " | ".join(_fmt(d[m]) for m in ABSOLUTE_METRICS)
                + " |"
            )
        lines.append("")
    usage = tool_usage(rows)
    if usage:
        lines += ["## Tool usage (calls, summed over trials)", ""]
        for v, d in usage.items():
            top = ", ".join(f"`{k}` {n}" for k, n in list(d.items())[:10])
            lines.append(f"- **{v}**: {top}")
        lines.append("")
    lines += [
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
            "## Regression check (paired (task, trial) cells, errors excluded)",
            "",
            "| suite | model | variant | baseline | variant | verdict |",
            "|---|---|---|---|---|---|",
        ]
        for reg in regs:
            gate = {"hold": "ok", "REGRESSION": "**REGRESSION**"}.get(reg["gate"], reg["gate"])
            lines.append(
                f"| {reg['suite']} | {reg['model']} | {reg['variant']} | {_rate(reg['baseline_rate'])} | "
                f"{_rate(reg['variant_rate'])} | {gate} |"
            )
    deltas = paired_deltas(rows, baseline)
    lines += ["", "## Deltas over tasks both variants passed (medians over passing trials with complete usage)", ""]
    if not deltas:
        lines.append("_No task passed in both variants yet – deltas withheld (they would reward failing faster)._")
    else:
        lines += [
            "| suite | task | model | variant | trials (base/variant) | tokens | tool-output tok (text) | "
            "shell-output tok | turns | agent s | cost |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for d in deltas:
            lines.append(
                f"| {d['suite']} | {d['task']} | {d['model']} | {d['variant']} | {d['n_base']}/{d['n_variant']} | "
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
            f"{row.status}{' (timeout)' if row.agent_timed_out else ''}{' (no usage)' if row.usage_missing else ''}"
            f"{' (partial usage)' if row.usage_partial else ''} | "
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
        "tool-output tokens ≈ chars/4 of text only (images in tool results are not counted). "
        f"Gate: paired (task, trial) cells, errors excluded; withheld above {MAX_ERROR_RATE:.0%} errors. "
        f"A token verdict needs gate `hold` and ≥ {MIN_PAIRED_TASKS} paired tasks. "
        "Token deltas exclude trials that timed out or have missing/partial usage. "
        "Bootstrap CIs use 1000 two-level resamples (tasks, then trials within task); seeds are deterministic._",
        "",
    ]
    return "\n".join(lines)
