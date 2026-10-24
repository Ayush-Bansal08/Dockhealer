"""Evaluation harness: measures the loop against fixed scenarios. It computes; it never invents.

Everything here is plain arithmetic over rows that came from real `heal()` runs. With a fake `heal_fn`
(tests) the same code runs, but those rows are labelled by whoever calls it: only runs on a real
Docker + Ollama machine produce numbers that may be quoted.
Metric definitions live in docs/EVAL_PROTOCOL.md and were written before any run.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import healer, scanner

VALID_KINDS = {"generate", "inject"}
VALID_OUTCOMES = {"healed", "repaired", "human_required"}


@dataclass(frozen=True)
class Scenario:
    id: str
    kind: str
    project: str
    expect_outcome: str
    dockerfile: str | None = None      # injected Dockerfile (inject) 
    reference: str | None = None       # hand-written baseline (generate)
    expect_category: str | None = None


def load_scenarios(manifest: str | Path, root: str | Path = ".") -> list[Scenario]:
    data = json.loads(Path(manifest).read_text())
    out, seen = [], set()
    for raw in data["scenarios"]:
        s = Scenario(**{k: v for k, v in raw.items()})
        if s.id in seen:
            raise ValueError(f"duplicate scenario id {s.id}")
        seen.add(s.id)
        if s.kind not in VALID_KINDS or s.expect_outcome not in VALID_OUTCOMES:
            raise ValueError(f"bad kind/outcome in scenario {s.id}")
        if s.kind == "inject" and not s.dockerfile:
            raise ValueError(f"inject scenario {s.id} needs a dockerfile")
        for rel in (s.project, s.dockerfile, s.reference):
            if rel and not (Path(root) / rel).exists():
                raise FileNotFoundError(f"scenario {s.id}: {rel} does not exist")
        out.append(s)
    return out


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion. Honest about tiny samples (n=3 gives a very wide range)."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def _first_category(report) -> str | None:
    a = report.attempts[0] if report.attempts else None
    if a is None:
        return None
    if a.outcome == "invalid":
        return "VALIDATION"
    return (a.diagnosis or {}).get("category")


def _healthy(a) -> bool:
    """An attempt counts as healthy only if it built AND (when a smoke test ran) the container answered."""
    return a.outcome == "built" and (a.smoke is None or a.smoke.get("ok") or a.smoke.get("skipped"))


def summarize_report(s: Scenario, trial: int, report, condition: str) -> dict:
    last_build = next((a.build for a in reversed(report.attempts) if a.build and a.build.get("success")), None)
    last = report.attempts[-1] if report.attempts else None
    scan = last.scan if last and last.scan and last.scan.get("ok") else None
    first = report.attempts[0] if report.attempts else None
    return {
        "scenario": s.id, "kind": s.kind, "trial": trial, "condition": condition,
        "success": report.success, "stop_reason": report.stop_reason,
        "first_attempt_healthy": bool(first and _healthy(first)),
        "first_failure_category": None if (first and _healthy(first)) else _first_category(report),
        "expect_category": s.expect_category, "expect_outcome": s.expect_outcome,
        "attempts": len(report.attempts), "llm_calls": report.llm_calls, "builds": report.builds,
        "build_duration_s": last_build["duration_s"] if last_build and report.success else None,
        "image_size_bytes": last_build["image_size_bytes"] if last_build and report.success else None,
        "smoke_ok": (last.smoke or {}).get("ok") if last and last.smoke else None,
        "scan_high_critical": (scan or {}).get("total") if scan else None,
        "duration_s": report.duration_s, "model": report.model, "detail": report.detail,
    }


def run_eval(scenarios: list[Scenario], *, trials: int, heal_fn: Callable, root: str | Path = ".",
             max_attempts: int = 3, only: set[str] | None = None, say: Callable[[str], None] = lambda _m: None,
             include_reference: bool = True) -> list[dict]:
    """heal_fn(info, *, initial_dockerfile, max_attempts) -> HealReport. Returns one row per run."""
    rows: list[dict] = []
    for s in scenarios:
        if only and s.id not in only:
            continue
        info = scanner.scan(Path(root) / s.project)
        if s.kind == "generate":
            if include_reference and s.reference:                      # hand-written baseline: no model, no repair
                ref = (Path(root) / s.reference).read_text()
                say(f"{s.id}: reference baseline")
                rows.append(summarize_report(s, 1, heal_fn(info, initial_dockerfile=ref, max_attempts=0), "reference"))
            for t in range(1, trials + 1):
                say(f"{s.id}: trial {t}/{trials}")
                rows.append(summarize_report(s, t, heal_fn(info, initial_dockerfile=None, max_attempts=max_attempts), "dockhealer"))
        else:
            df = (Path(root) / s.dockerfile).read_text()
            for t in range(1, trials + 1):
                say(f"{s.id}: trial {t}/{trials}")
                rows.append(summarize_report(s, t, heal_fn(info, initial_dockerfile=df, max_attempts=max_attempts), "dockhealer"))
    return rows


# ---- aggregation -------------------------------------------------------------------------------
def _rate(k: int, n: int) -> dict:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "rate": (k / n) if n else None, "ci95": [round(lo, 3), round(hi, 3)]}


def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 2) if xs else None


def aggregate(rows: list[dict]) -> dict:
    gen = [r for r in rows if r["kind"] == "generate" and r["condition"] == "dockhealer"]
    ref = [r for r in rows if r["condition"] == "reference"]
    inj = [r for r in rows if r["kind"] == "inject"]
    repair_cases = [r for r in inj if r["expect_outcome"] == "repaired"]
    stop_cases = [r for r in inj if r["expect_outcome"] == "human_required"]
    ok_gen = [r for r in gen if r["success"]]
    return {
        "generation": {
            "runs": len(gen),
            "one_shot_success": _rate(sum(r["first_attempt_healthy"] for r in gen), len(gen)),
            "success_with_repair": _rate(len(ok_gen), len(gen)),
            "repair_rescued": sum(1 for r in gen if r["success"] and not r["first_attempt_healthy"]),
            "median_model_calls_when_healed": _median([r["llm_calls"] for r in ok_gen]),
            "median_build_s": _median([r["build_duration_s"] for r in ok_gen]),
            "median_image_mb": _median([r["image_size_bytes"] / 1e6 for r in ok_gen if r["image_size_bytes"]]),
            "smoke_pass": _rate(sum(1 for r in gen if r["smoke_ok"]), sum(1 for r in gen if r["smoke_ok"] is not None)),
            "median_trivy_high_critical": _median([r["scan_high_critical"] for r in ok_gen]),
        },
        "reference": {
            "runs": len(ref),
            "healthy": _rate(sum(1 for r in ref if r["success"]), len(ref)),
            "median_build_s": _median([r["build_duration_s"] for r in ref if r["success"]]),
            "median_image_mb": _median([r["image_size_bytes"] / 1e6 for r in ref if r["success"] and r["image_size_bytes"]]),
            "median_trivy_high_critical": _median([r["scan_high_critical"] for r in ref if r["success"]]),
        },
        "injection": {
            "runs": len(inj),
            "fault_was_real": _rate(sum(1 for r in inj if not r["first_attempt_healthy"]), len(inj)),
            "classified_as_expected": _rate(sum(1 for r in inj if r["first_failure_category"] == r["expect_category"]), len(inj)),
            "repaired": _rate(sum(1 for r in repair_cases if r["success"]), len(repair_cases)),
            "median_model_calls_repaired": _median([r["llm_calls"] for r in repair_cases if r["success"]]),
            "stopped_correctly": _rate(sum(1 for r in stop_cases if r["stop_reason"] == healer.HUMAN_REQUIRED and r["llm_calls"] == 0), len(stop_cases)),
        },
    }


def _pct(m: dict) -> str:
    if not m["n"]:
        return "n/a (no runs)"
    return f"{m['k']}/{m['n']} = {m['rate']:.0%} (95% CI {m['ci95'][0]:.0%}-{m['ci95'][1]:.0%})"


def render_markdown(rows: list[dict], meta: dict) -> str:
    a = aggregate(rows)
    g, r, i = a["generation"], a["reference"], a["injection"]
    per = {}
    for row in rows:
        per.setdefault((row["scenario"], row["condition"]), []).append(row)
    lines = ["# DockHealer evaluation report", "",
             f"- model: `{meta.get('model')}`  trials per scenario: {meta.get('trials')}  max model calls: {meta.get('max_attempts')}",
             f"- smoke test: {meta.get('smoke')}  trivy: {meta.get('scan')}  date: {meta.get('date')}",
             f"- environment: {meta.get('environment', 'unspecified')}",
             f"- provenance: **{meta.get('provenance', 'UNSPECIFIED')}**", "",
             "Read with docs/EVAL_PROTOCOL.md. Small samples: intervals are wide on purpose; do not over-read differences.", "",
             "## Generation (3 example apps)", "",
             f"- one-shot healthy (first attempt built and smoke-tested): {_pct(g['one_shot_success'])}",
             f"- healthy with up to {meta.get('max_attempts')} model calls: {_pct(g['success_with_repair'])}",
             f"- runs rescued by repair: {g['repair_rescued']} of {g['runs']}",
             f"- median model calls when healed: {g['median_model_calls_when_healed']}; median build s: {g['median_build_s']}; "
             f"median image MB: {g['median_image_mb']}",
             f"- smoke pass among runs that reached a smoke test: {_pct(g['smoke_pass'])}",
             f"- median Trivy HIGH/CRITICAL (known CVEs only, not a security verdict): {g['median_trivy_high_critical']}", "",
             "## Hand-written reference Dockerfiles (no model)", "",
             f"- healthy: {_pct(r['healthy'])}; median build s: {r['median_build_s']}; median image MB: {r['median_image_mb']}; "
             f"median Trivy HIGH/CRITICAL: {r['median_trivy_high_critical']}", "",
             "## Failure injection", "",
             f"- injected fault really broke the first attempt: {_pct(i['fault_was_real'])}",
             f"- first failure classified as expected category: {_pct(i['classified_as_expected'])}",
             f"- repairable faults repaired: {_pct(i['repaired'])}; median model calls: {i['median_model_calls_repaired']}",
             f"- must-stop faults stopped with no model call: {_pct(i['stopped_correctly'])}", "",
             "## Per scenario", "", "| scenario | condition | runs | healthy | first failure | stop reasons | model calls |", "|---|---|---|---|---|---|---|"]
    for (sid, cond), rs in per.items():
        cats = sorted({str(x["first_failure_category"]) for x in rs if x["first_failure_category"]}) or ["-"]
        stops = sorted({x["stop_reason"] for x in rs})
        lines.append(f"| {sid} | {cond} | {len(rs)} | {sum(x['success'] for x in rs)}/{len(rs)} | {', '.join(cats)} | "
                     f"{', '.join(stops)} | {sorted(x['llm_calls'] for x in rs)} |")
    lines += ["", "## Not measured / limits", "",
              "- Only 3 tiny apps and 9 injected faults: this shows behaviour on this set, not general success rates.",
              "- Smoke test proves the container answered HTTP, not that the app is correct. Trivy lists known CVEs only.",
              "- Model output can vary run to run even with temperature 0; trials expose some of that, not all.",
              "- Results depend on this machine, Docker/Ollama versions and network."]
    return "\n".join(lines) + "\n"


def write_results(rows: list[dict], meta: dict, out: str | Path) -> tuple[Path, Path]:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    jl = out / "results.jsonl"
    jl.write_text("".join(json.dumps(r) + "\n" for r in rows))
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    md = out / "report.md"
    md.write_text(render_markdown(rows, meta))
    return jl, md
