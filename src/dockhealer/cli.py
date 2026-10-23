"""Command-line entry point. Kept thin: parse args, call library code, print, return an exit code."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from . import __version__, builder, diagnose, doctor, evaluate, generator, healer, scanner, smoke, validator, vulnscan
from .config import Settings
from pathlib import Path

from .llm import LLMError, OllamaClient

ICON = {doctor.OK: "[ OK ]", doctor.WARN: "[WARN]", doctor.FAIL: "[FAIL]"}


def cmd_doctor(args: argparse.Namespace) -> int:
    results = doctor.run_all(OllamaClient(Settings.from_env()))
    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        for r in results:
            print(f"{ICON[r.status]} {r.name:<7} {r.detail}")
            if r.hint and r.status != doctor.OK:
                print(f"         -> {r.hint}")
    return 1 if any(r.status == doctor.FAIL for r in results) else 0


def cmd_ask(args: argparse.Namespace) -> int:
    client = OllamaClient(Settings.from_env())
    try:
        res = client.chat(args.prompt, model=args.model)
    except LLMError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(res.content)
    print(f"\n[{res.model} | {res.output_tokens} tokens | {res.duration_s:.1f}s]", file=sys.stderr)
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    try:
        info = scanner.scan(args.path)
    except NotADirectoryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(asdict(info), indent=2))
    else:
        print(f"language:   {info.language}  framework: {info.framework}")
        print(f"entrypoint: {info.entrypoint}  port: {info.port} ({info.port_source})")
        print(f"start:      {info.start_command}")
        print(f"files sent: {len(info.files)}   excluded: {len(info.excluded)}")
        for path, reason in info.excluded:
            print(f"  excluded {path}  <- {reason}")
        for w in info.warnings:
            print(f"WARNING: {w}")
    if args.context:
        ctx, n = scanner.build_context(info)
        print(f"\n===== MODEL CONTEXT ({n} redactions) =====\n{ctx}")
    return 0 if info.supported else 1


def cmd_validate(args: argparse.Namespace) -> int:
    info = scanner.scan(args.project) if args.project else None
    try:
        text = Path(args.dockerfile).read_text()
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    res = validator.validate(text, info)
    print(validator.format_findings(res))
    print(f"result: {'VALID' if res.ok else 'INVALID'} ({len(res.errors)} errors, {len(res.warnings)} warnings)")
    return 0 if res.ok else 3


def cmd_generate(args: argparse.Namespace) -> int:
    info = scanner.scan(args.path)
    if not info.supported:
        print("error: unsupported project (need requirements.txt/pyproject.toml or package.json)", file=sys.stderr)
        return 1
    client = OllamaClient(Settings.from_env())
    try:
        gen = generator.generate(client, info)
    except (LLMError, generator.GenerationError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    res = validator.validate(gen.dockerfile, info)
    print(gen.dockerfile)
    print(validator.format_findings(res), file=sys.stderr)
    print(f"[{gen.llm.model} | {gen.llm.output_tokens} tokens | {gen.llm.duration_s:.1f}s | "
          f"{'VALID' if res.ok else 'INVALID'}]", file=sys.stderr)
    if res.ok and args.out:
        try:
            print(f"wrote {generator.write_dockerfile(gen.dockerfile, args.out, force=args.force)}", file=sys.stderr)
        except FileExistsError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    elif args.out:
        print("not written: Dockerfile failed validation", file=sys.stderr)
    return 0 if res.ok else 3


def cmd_build(args: argparse.Namespace) -> int:
    info = scanner.scan(args.path)
    if not info.supported:
        print("error: unsupported project", file=sys.stderr)
        return 1
    df_path = Path(args.dockerfile) if args.dockerfile else Path(args.path) / "Dockerfile"
    try:
        text = df_path.read_text()
    except OSError as e:
        print(f"error: cannot read Dockerfile ({e}). Create one with `dockhealer generate --out DIR`.", file=sys.stderr)
        return 2
    check = validator.validate(text, info)  # deterministic gate: do not spend a build on a file we know is broken
    if not check.ok and not args.skip_validation:
        print(validator.format_findings(check))
        print("refusing to build: Dockerfile failed validation (override with --skip-validation)", file=sys.stderr)
        return 3
    try:
        res = builder.run_build(info, text, timeout_s=args.timeout)
    except (builder.DockerUnavailable, builder.ContextTooLarge) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.save_log:
        Path(args.save_log).write_text(diagnose.log_for_model(res.log, max_lines=100000))  # redacted copy
    if res.success:
        mb = (res.image_size_bytes or 0) / 1e6
        print(f"BUILD OK  {res.duration_s:.1f}s  image {res.tag}  {mb:.0f} MB")
        if not args.keep_image:
            builder.remove_image(res.tag)
            print("(image removed; use --keep-image to keep it)")
        return 0
    d = diagnose.diagnose(res)
    print(f"BUILD FAILED after {res.duration_s:.1f}s")
    print(f"category: {d.category}   next action: {d.action}")
    print(f"summary:  {d.summary}")
    if d.failed_step:
        print(f"failed step: {d.failed_step}" + (f"   (Dockerfile line {d.failed_line})" if d.failed_line else ""))
    for line in d.evidence:
        print(f"  evidence: {line}")
    print(f"hint: {d.hint}")
    return 4


def _bounded(lo: int, hi: int):
    def parse(text: str) -> int:
        value = int(text)
        if not lo <= value <= hi:
            raise argparse.ArgumentTypeError(f"must be between {lo} and {hi}")
        return value
    return parse


def cmd_heal(args: argparse.Namespace) -> int:
    try:
        info = scanner.scan(args.path)
    except NotADirectoryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not info.supported:
        print("error: unsupported project (need requirements.txt/pyproject.toml or package.json)", file=sys.stderr)
        return 1
    client = OllamaClient(Settings.from_env())
    report = healer.heal(info, client, max_attempts=args.max_attempts, max_retries=args.max_retries,
                         build_timeout_s=args.timeout, keep_image=args.keep_image,
                         smoke_fn=None if args.no_smoke else smoke.run_smoke,
                         scan_fn=vulnscan.to_dict_scan if args.scan else None,
                         say=lambda m: print(m, flush=True))
    report_dir = Path(args.report_dir) if args.report_dir else healer.default_report_dir(info)
    healer.write_report(report, report_dir)
    print("\n--- attempts ---")
    for a in report.attempts:
        extra = ""
        if a.outcome == "invalid":
            extra = " (" + ", ".join(sorted({f["rule"] for f in a.findings if f["severity"] == "error"})) + ")"
        elif a.diagnosis:
            extra = f" ({a.diagnosis.get('category')})"
        print(f"  {a.number}. {a.kind:<8} -> {a.outcome}{extra}")
    verdict = "HEALED" if report.success else "NOT HEALED"
    print(f"\nRESULT: {verdict}   stop reason: {report.stop_reason}"
          + (f" ({report.detail})" if report.detail else ""))
    print(f"model calls: {report.llm_calls}   builds: {report.builds}   time: {report.duration_s:.0f}s   report: {report_dir}/report.json")
    last = report.attempts[-1] if report.attempts else None
    if last and last.smoke:
        sm = last.smoke
        print("smoke test: " + ("SKIPPED (" + sm["detail"] + ")" if sm["skipped"] else
              f"{'PASS' if sm['ok'] else 'FAIL'}  GET {sm['path']} -> {sm['status']}" if sm["ok"] else "FAIL"))
    if last and last.scan:
        print("vulnerability scan: " + vulnscan.summarize(vulnscan.ScanResult(**last.scan)))
    if report.success and args.out:
        try:
            print(f"wrote {generator.write_dockerfile(report.final_dockerfile, args.out, force=args.force)}")
        except FileExistsError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    elif not report.success:
        print("A person needs to look at this: see the last attempt in the report.")
    return 0 if report.success else 4


def _severities(text: str) -> tuple[str, ...]:
    parts = tuple(t.strip().upper() for t in text.split(",") if t.strip())
    bad = [t for t in parts if t not in vulnscan.SEVERITY_ORDER]
    if not parts or bad:
        raise argparse.ArgumentTypeError(f"use a comma list of {','.join(vulnscan.SEVERITY_ORDER)}")
    return parts


def cmd_smoke(args: argparse.Namespace) -> int:
    try:
        info = scanner.scan(args.project)
        res = smoke.run_smoke(args.image, info, startup_timeout_s=args.timeout)
    except (NotADirectoryError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if res.skipped:
        print(f"SKIPPED: {res.detail}")
        return 5
    if res.ok:
        print(f"SMOKE OK  GET {res.path} -> {res.status}  in {res.duration_s:.1f}s")
        return 0
    d = smoke.diagnose_smoke(res, info)
    print(f"SMOKE FAILED: {res.detail}")
    print(f"category: {d.category}   next action: {d.action}")
    print(f"summary:  {d.summary}")
    for line in d.evidence:
        print(f"  evidence: {line}")
    print(f"hint: {d.hint}")
    return 4


def cmd_scan(args: argparse.Namespace) -> int:
    res = vulnscan.scan_image(args.image, severities=args.severity)
    print(vulnscan.summarize(res))
    if not (res.available and res.ok):
        return 2
    for v in res.top:
        print(f"  {v['severity']:<8} {v['id']:<16} {v['package']} {v['installed']}" + (f" -> fixed in {v['fixed']}" if v["fixed"] else " (no fix)"))
    if args.fail_on:
        rank = {s: i for i, s in enumerate(vulnscan.SEVERITY_ORDER)}
        if any(rank[sev] <= rank[args.fail_on] for sev in res.counts):
            print(f"FAIL: findings at or above {args.fail_on}")
            return 4
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    report = evaluate.evaluate(args.root, build=args.build, timeout_s=args.timeout)
    if args.report:
        evaluate.write_report(report, args.report)
    print("BASELINES")
    for case in report.baselines:
        print(f"  {case.name:<42} {'PASS' if case.validation_ok else 'FAIL'}")
    print("FAILURE INJECTIONS")
    for case in report.failures:
        if not case.validation_ok:
            status = "CAUGHT BY VALIDATOR"
        elif case.build_success is False:
            status = f"CAUGHT BY BUILD ({case.diagnosis or 'UNKNOWN'})"
        elif case.build_success is True:
            status = "REGRESSION: BUILD SUCCEEDED"
        else:
            status = "NOT BUILT"
        print(f"  {case.name:<42} {status}")
    mode = "with Docker builds" if args.build else "validation-only"
    print(f"\nEVALUATION: {'PASS' if report.success else 'FAIL'} ({mode})")
    if args.report:
        print(f"report: {args.report}")
    return 0 if report.success else 4


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dockhealer", description=__doc__)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="check RAM, Docker, Ollama and model availability")
    d.add_argument("--json", action="store_true", help="machine-readable output")
    d.set_defaults(func=cmd_doctor)

    i = sub.add_parser("inspect", help="detect stack, port and start command of a project")
    i.add_argument("path")
    i.add_argument("--json", action="store_true")
    i.add_argument("--context", action="store_true", help="also print exactly what the model would see")
    i.set_defaults(func=cmd_inspect)

    v = sub.add_parser("validate", help="lint a Dockerfile (no model involved)")
    v.add_argument("dockerfile")
    v.add_argument("--project", help="project directory, enables COPY-path and entrypoint checks")
    v.set_defaults(func=cmd_validate)

    g = sub.add_parser("generate", help="generate a Dockerfile with the local model, then validate it")
    g.add_argument("path")
    g.add_argument("--out", help="directory to write Dockerfile into (only if valid)")
    g.add_argument("--force", action="store_true", help="overwrite an existing Dockerfile")
    g.set_defaults(func=cmd_generate)

    b = sub.add_parser("build", help="validate, then docker-build a project's Dockerfile and diagnose failures")
    b.add_argument("path")
    b.add_argument("--dockerfile", help="Dockerfile to build (default: PATH/Dockerfile)")
    b.add_argument("--timeout", type=float, default=builder.DEFAULT_TIMEOUT_S, help="seconds before the build is killed")
    b.add_argument("--keep-image", action="store_true")
    b.add_argument("--skip-validation", action="store_true")
    b.add_argument("--save-log", help="write the (redacted) build log here, e.g. to capture a real test fixture")
    b.set_defaults(func=cmd_build)

    h = sub.add_parser("heal", help="generate -> validate -> build -> diagnose -> repair, with hard limits")
    h.add_argument("path")
    h.add_argument("--max-attempts", type=_bounded(1, 10), default=3, help="max model calls (1 generation + repairs), 1-10")
    h.add_argument("--max-retries", type=_bounded(0, 3), default=1, help="unchanged rebuilds after a network failure, 0-3")
    h.add_argument("--timeout", type=float, default=builder.DEFAULT_TIMEOUT_S, help="seconds per docker build")
    h.add_argument("--out", help="write the final Dockerfile into this directory (only if it built)")
    h.add_argument("--force", action="store_true", help="overwrite an existing Dockerfile in --out")
    h.add_argument("--keep-image", action="store_true")
    h.add_argument("--no-smoke", action="store_true", help="skip starting the container to test it")
    h.add_argument("--scan", action="store_true", help="scan the finished image with Trivy (if installed)")
    h.add_argument("--report-dir", help="where to write report.json (default: runs/<time>-<app>/)")
    h.set_defaults(func=cmd_heal)

    sm = sub.add_parser("smoke", help="start an existing local image and check that it answers on its port")
    sm.add_argument("image")
    sm.add_argument("--project", required=True, help="project directory (tells us which port to probe)")
    sm.add_argument("--timeout", type=float, default=smoke.STARTUP_TIMEOUT_S, help="seconds to wait for an answer")
    sm.set_defaults(func=cmd_smoke)

    sc = sub.add_parser("scan", help="scan a local image for known vulnerabilities with Trivy")
    sc.add_argument("image")
    sc.add_argument("--severity", type=_severities, default=("HIGH", "CRITICAL"), help="comma list, default HIGH,CRITICAL")
    sc.add_argument("--fail-on", choices=vulnscan.SEVERITY_ORDER, help="exit 4 if any finding is at/above this severity")
    sc.set_defaults(func=cmd_scan)

    ev = sub.add_parser("evaluate", help="run the baseline and failure-injection evaluation")
    ev.add_argument("--root", default=".", help="repository root (default: current directory)")
    ev.add_argument("--build", action="store_true", help="also build valid cases with Docker")
    ev.add_argument("--timeout", type=float, default=builder.DEFAULT_TIMEOUT_S)
    ev.add_argument("--report", help="write a machine-readable JSON report")
    ev.set_defaults(func=cmd_evaluate)

    a = sub.add_parser("ask", help="send one prompt to the local model (smoke test)")
    a.add_argument("prompt")
    a.add_argument("--model", default=None)
    a.set_defaults(func=cmd_ask)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
