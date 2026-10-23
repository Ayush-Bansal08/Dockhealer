"""The bounded feedback loop:  generate -> validate -> build -> diagnose -> repair -> ...

Every decision about WHETHER to continue is made here, in plain deterministic code. The model only
supplies candidate Dockerfiles. The loop is guaranteed to end because every pass either returns or
consumes one of two finite budgets:
    max_attempts  - number of model calls (1 generation + repairs)
    max_retries   - number of unchanged rebuilds after a transient (network) failure
and it also stops early on: a human-only diagnosis, a repeated failure, no-progress (the model returned a
Dockerfile it already tried), an unusable environment, or a model error.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import builder, diagnose, generator, repair, smoke, validator, vulnscan
from .llm import LLMError
from .scanner import ProjectInfo, redact

# stop reasons (stable strings: they appear in reports and in the evaluation)
BUILT = "built"
BUDGET_EXHAUSTED = "budget_exhausted"
HUMAN_REQUIRED = "human_required"
NO_PROGRESS = "no_progress"
REPEATED_FAILURE = "repeated_failure"
LLM_ERROR = "llm_error"
ENVIRONMENT = "environment"


@dataclass
class Attempt:
    number: int
    kind: str                      # generate | repair | retry
    outcome: str                   # built | invalid | build_failed | no_dockerfile
    dockerfile: str = ""
    findings: list[dict] = field(default_factory=list)
    build: dict | None = None
    diagnosis: dict | None = None
    llm: dict | None = None
    new_warnings: list[str] = field(default_factory=list)   # warning rules a repair introduced
    smoke: dict | None = None
    scan: dict | None = None


@dataclass
class HealReport:
    project: str
    success: bool
    stop_reason: str
    attempts: list[Attempt]
    final_dockerfile: str
    llm_calls: int
    builds: int
    duration_s: float
    model: str | None = None
    detail: str = ""


def _norm(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def _fingerprint(d: diagnose.Diagnosis) -> tuple:
    return (d.category, d.failed_step or (d.evidence[0] if d.evidence else ""))


def _finding_dicts(res: validator.ValidationResult) -> list[dict]:
    return [{"rule": f.rule, "severity": f.severity, "line": f.line, "message": f.message} for f in res.findings]


def _llm_dict(chat) -> dict:
    return {"model": chat.model, "output_tokens": chat.output_tokens, "duration_s": round(chat.duration_s, 2)}


def heal(info: ProjectInfo, client, *, max_attempts: int = 3, max_retries: int = 1,
         build_timeout_s: float = builder.DEFAULT_TIMEOUT_S, keep_image: bool = False,
         build_fn: Callable = builder.run_build, remove_fn: Callable = builder.remove_image,
         smoke_fn: Callable | None = None, scan_fn: Callable | None = None,
         clock: Callable[[], float] = time.monotonic, say: Callable[[str], None] = lambda _m: None) -> HealReport:
    start = clock()
    attempts: list[Attempt] = []
    llm_calls = builds = retries = 0
    seen_dockerfiles: set[str] = set()
    seen_failures: set[tuple] = set()
    current: str | None = None      # last candidate Dockerfile
    problem: str = ""               # what the model is told on the next repair
    step = "generate"               # generate | repair | retry
    model = None
    prev_warning_rules: set[str] = set()

    def finish(success: bool, reason: str, detail: str = "") -> HealReport:
        return HealReport(project=info.root, success=success, stop_reason=reason, attempts=attempts,
                          final_dockerfile=current or "", llm_calls=llm_calls, builds=builds,
                          duration_s=round(clock() - start, 2), model=model, detail=detail)

    while True:
        n = len(attempts) + 1
        attempt = Attempt(number=n, kind=step, outcome="")

        # ---- 1. obtain a candidate Dockerfile -------------------------------------------------
        if step in ("generate", "repair"):
            if llm_calls >= max_attempts:
                return finish(False, BUDGET_EXHAUSTED, f"used all {max_attempts} model attempts")
            llm_calls += 1
            say(f"attempt {n} [{step}]: asking the model ...")
            try:
                if step == "generate":
                    gen = generator.generate(client, info)
                    candidate, chat = gen.dockerfile, gen.llm
                else:
                    fixed = repair.repair_dockerfile(client, info, current or "", problem)
                    candidate, chat = fixed.dockerfile, fixed.llm
            except (generator.GenerationError, repair.RepairError) as e:
                attempt.outcome = "no_dockerfile"
                attempt.diagnosis = {"category": "NO_DOCKERFILE", "summary": str(e)}
                attempts.append(attempt)
                say(f"  model reply contained no Dockerfile ({e})")
                continue                                  # consumed one attempt; budget guarantees termination
            except LLMError as e:
                return finish(False, LLM_ERROR, str(e))
            model = chat.model
            attempt.llm = _llm_dict(chat)
            if step == "repair" and _norm(candidate) in seen_dockerfiles:
                attempt.dockerfile, attempt.outcome = candidate, "no_progress"
                attempts.append(attempt)
                current = candidate
                return finish(False, NO_PROGRESS, "the model returned a Dockerfile it had already tried")
            current = candidate
        else:  # retry: identical Dockerfile, no model call
            candidate = current or ""
            say(f"attempt {n} [retry]: rebuilding the same Dockerfile after a transient failure ...")
        attempt.dockerfile = candidate
        seen_dockerfiles.add(_norm(candidate))

        # ---- 2. deterministic validation (free, instant) --------------------------------------
        check = validator.validate(candidate, info)
        attempt.findings = _finding_dicts(check)
        warning_rules = {f.rule for f in check.warnings}
        if step == "repair":
            attempt.new_warnings = sorted(warning_rules - prev_warning_rules)
        prev_warning_rules = warning_rules
        if not check.ok:
            attempt.outcome = "invalid"
            attempts.append(attempt)
            say(f"  validator rejected it: {', '.join(sorted({f.rule for f in check.errors}))}")
            fp = ("VALIDATION", tuple(sorted(f.rule for f in check.errors)))
            if fp in seen_failures:
                return finish(False, REPEATED_FAILURE, "the same validation errors came back after a repair")
            seen_failures.add(fp)
            problem, step = repair.describe_validation(check), "repair"
            continue

        # ---- 3. build (slow, bounded) ---------------------------------------------------------
        say(f"  validator OK; building (timeout {build_timeout_s:.0f}s) ...")
        try:
            result = build_fn(info, candidate, timeout_s=build_timeout_s)
        except (builder.DockerUnavailable, builder.ContextTooLarge) as e:
            attempt.outcome = "build_failed"
            attempt.diagnosis = {"category": "ENVIRONMENT", "action": diagnose.STOP, "summary": str(e)}
            attempts.append(attempt)
            return finish(False, ENVIRONMENT, str(e))
        builds += 1
        attempt.build = {"success": result.success, "returncode": result.returncode,
                         "duration_s": round(result.duration_s, 2), "timed_out": result.timed_out,
                         "image_size_bytes": result.image_size_bytes, "tag": result.tag}
        problem_text = None
        if result.success:
            sr = None
            if smoke_fn is not None:
                say("  build OK; starting the container for a smoke test ...")
                sr = smoke_fn(result.tag, info)
                attempt.smoke = {"ok": sr.ok, "skipped": sr.skipped, "status": sr.status, "path": sr.path,
                                 "container_exited": sr.container_exited, "exit_code": sr.exit_code,
                                 "duration_s": round(sr.duration_s, 2), "detail": sr.detail}
            if sr is None or sr.ok or sr.skipped:
                if scan_fn is not None:
                    say("  scanning the image for known vulnerabilities ...")
                    try:
                        attempt.scan = scan_fn(result.tag)
                    except Exception as e:  # noqa: BLE001 - a scanner problem must never fail or crash a healed build
                        attempt.scan = {"available": True, "ok": False, "detail": f"scan error: {e}"}
                attempt.outcome = "built"
                attempts.append(attempt)
                if not keep_image:
                    remove_fn(result.tag)
                say("  BUILD OK" + ("" if sr is None else (" (smoke test skipped)" if sr.skipped else " + SMOKE OK")))
                return finish(True, BUILT)
            if not keep_image:
                remove_fn(result.tag)
            d = smoke.diagnose_smoke(sr, info)
            problem_text = repair.describe_smoke_failure(sr, d)
            attempt.outcome = "smoke_failed"
        else:
            d = diagnose.diagnose(result)
            problem_text = repair.describe_build_failure(result, d)
            attempt.outcome = "build_failed"

        # ---- 4. diagnose and decide (build failures and smoke-test failures share this path) ----
        attempt.diagnosis = {"category": d.category, "action": d.action, "summary": d.summary, "hint": d.hint,
                             "failed_step": d.failed_step, "failed_line": d.failed_line,
                             "evidence": [redact(e)[0] for e in d.evidence],
                             "log_tail": diagnose.log_for_model(result.log if not result.success else sr.logs, max_lines=40)}
        attempts.append(attempt)
        say(f"  {'container test' if result.success else 'build'} failed: {d.category} -> {d.action}")

        if d.action == diagnose.STOP:
            return finish(False, HUMAN_REQUIRED, f"{d.category}: {d.summary}")
        if d.action == diagnose.RETRY:
            if retries >= max_retries:
                return finish(False, HUMAN_REQUIRED, f"{d.category} persisted after {retries} retry")
            retries += 1
            step = "retry"
            continue
        fp = _fingerprint(d)                      # action == REPAIR
        if fp in seen_failures:
            return finish(False, REPEATED_FAILURE, f"{d.category} came back after a repair")
        seen_failures.add(fp)
        problem, step = problem_text, "repair"


# ---------------------------------------------------------------------------------------------
def report_to_dict(report: HealReport) -> dict:
    return asdict(report)


def write_report(report: HealReport, directory: str | Path) -> Path:
    """Write report.json and one Dockerfile per attempt. Anything log-derived was redacted upstream."""
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    for a in report.attempts:
        if a.dockerfile:
            (out / f"Dockerfile.attempt{a.number}").write_text(a.dockerfile)
    path = out / "report.json"
    path.write_text(json.dumps(report_to_dict(report), indent=2))
    return path


def default_report_dir(info: ProjectInfo, base: str | Path = "runs") -> Path:
    name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", Path(info.root).name) or "app"
    return Path(base) / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}"
