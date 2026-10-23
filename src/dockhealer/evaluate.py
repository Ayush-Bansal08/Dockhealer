"""Offline evaluation of reference Dockerfiles and injected build failures.

The evaluation deliberately separates cheap validation from an optional Docker build. This makes
the experiment reproducible on a laptop without Docker while still allowing real build results to
be captured when a daemon is available.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import builder, diagnose, scanner, validator


@dataclass
class CaseResult:
    name: str
    app: str
    kind: str
    dockerfile: str
    validation_ok: bool
    findings: list[dict] = field(default_factory=list)
    build_attempted: bool = False
    build_success: bool | None = None
    diagnosis: str | None = None
    detail: str = ""


@dataclass
class EvaluationReport:
    success: bool
    docker_available: bool
    baselines: list[CaseResult]
    failures: list[CaseResult]


def _case_app(path: Path, kind: str) -> str:
    if kind == "baseline":
        return path.stem
    return path.name.split("__", 1)[0]


def _finding_dicts(result: validator.ValidationResult) -> list[dict]:
    return [{"rule": f.rule, "severity": f.severity, "line": f.line, "message": f.message}
            for f in result.findings]


def _load_case(path: Path, root: Path, kind: str) -> tuple[str, scanner.ProjectInfo, str]:
    app = _case_app(path, kind)
    candidates = (app, f"{app}_app")
    app_dir = next((root / "examples" / candidate for candidate in candidates
                    if (root / "examples" / candidate).is_dir()), None)
    if app_dir is None:
        raise NotADirectoryError(f"no example project for evaluation case {path.name}")
    app = app_dir.name
    info = scanner.scan(app_dir)
    return app, info, path.read_text()


def evaluate(root: str | Path, *, build: bool = False, build_fn: Callable = builder.run_build,
             remove_fn: Callable = builder.remove_image, timeout_s: float = builder.DEFAULT_TIMEOUT_S) -> EvaluationReport:
    """Evaluate baselines and injected failures.

    A baseline must validate cleanly. An injected failure must either be rejected by validation or
    fail its build; a successful build is a regression in the test corpus. Docker errors are
    reported in the case and do not make an offline validation-only run fail.
    """
    root = Path(root)
    baseline_paths = sorted((root / "baselines").glob("*.Dockerfile"))
    failure_paths = sorted((root / "failures").glob("*.Dockerfile"))
    baselines: list[CaseResult] = []
    failures: list[CaseResult] = []
    docker_available = not build

    for path, kind, output in [(p, "baseline", baselines) for p in baseline_paths] + [
        (p, "failure", failures) for p in failure_paths
    ]:
        app, info, text = _load_case(path, root, kind)
        check = validator.validate(text, info)
        result = CaseResult(path.name, app, kind, str(path.relative_to(root)), check.ok, _finding_dicts(check))
        if build and check.ok:
            result.build_attempted = True
            try:
                built = build_fn(info, text, timeout_s=timeout_s)
                result.build_success = built.success
                if built.success:
                    remove_fn(built.tag)
                else:
                    result.diagnosis = diagnose.diagnose(built).category
            except (builder.DockerUnavailable, builder.ContextTooLarge) as exc:
                docker_available = False
                result.detail = str(exc)
        output.append(result)

    baseline_ok = all(case.validation_ok for case in baselines)
    failure_ok = all(not case.validation_ok or case.build_success is not True for case in failures)
    return EvaluationReport(baseline_ok and failure_ok, docker_available, baselines, failures)


def write_report(report: EvaluationReport, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(asdict(report), indent=2))
    return target
