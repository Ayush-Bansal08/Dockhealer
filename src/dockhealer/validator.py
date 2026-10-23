"""Deterministic Dockerfile validator.

A language model produces *plausible* text, not *correct* text. This module is the gate between
the two: it is plain, testable code with no model in it. Findings have a severity:
  error - the build would fail, or the file is unsafe. Must be fixed before `docker build`.
  warn  - works but is wasteful or weak (root user, unpinned base image, poor layer caching).

Each rule has a stable id so reports, tests and the repair loop can refer to it.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .scanner import ProjectInfo

ERROR, WARN = "error", "warn"

INSTRUCTIONS = {"FROM", "RUN", "CMD", "LABEL", "MAINTAINER", "EXPOSE", "ENV", "ADD", "COPY", "ENTRYPOINT",
                "VOLUME", "USER", "WORKDIR", "ARG", "ONBUILD", "STOPSIGNAL", "HEALTHCHECK", "SHELL"}

PIPE_TO_SHELL = re.compile(r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da)?sh\b")
SECRET_NAME = re.compile(r"(?i)(password|passwd|secret|token|api_?key|private_?key)")
INSTALL_DEPS = re.compile(r"\b(pip3?\s+install|npm\s+(install|ci)|yarn\s+install|poetry\s+install)\b")


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    line: int
    message: str


@dataclass(frozen=True)
class Instruction:
    name: str
    args: str
    line: int


@dataclass
class ValidationResult:
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARN]

    @property
    def ok(self) -> bool:
        return not self.errors


def parse(text: str) -> tuple[list[Instruction], list[Finding]]:
    """Split a Dockerfile into instructions, joining backslash continuations."""
    instructions: list[Instruction] = []
    problems: list[Finding] = []
    buf, start = "", 0
    for n, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not buf and (not stripped or stripped.startswith("#")):
            continue
        if stripped.startswith("#"):  # comment inside a continued instruction
            continue
        if not buf:
            start = n
        if stripped.endswith("\\"):
            buf += stripped[:-1].rstrip() + " "
            continue
        full = (buf + stripped).strip()
        buf = ""
        name, _, args = full.partition(" ")
        if name.upper() not in INSTRUCTIONS:
            problems.append(Finding("E002", ERROR, start, f"'{name}' is not a Dockerfile instruction"))
            continue
        instructions.append(Instruction(name.upper(), args.strip(), start))
    if buf:
        problems.append(Finding("E002", ERROR, start, "file ends in the middle of a continued line"))
    return instructions, problems


def _stages(instrs: list[Instruction]) -> list[list[Instruction]]:
    stages: list[list[Instruction]] = []
    for i in instrs:
        if i.name == "FROM":
            stages.append([i])
        elif stages:
            stages[-1].append(i)
    return stages


def _exists(src: str, project_files: list[str]) -> bool:
    src = src.removeprefix("./").rstrip("/")
    if src in ("", "."):
        return True
    if any(c in src for c in "*?["):
        return any(fnmatch.fnmatch(f, src) for f in project_files)
    return src in project_files or any(f.startswith(src + "/") for f in project_files)


def _copy_sources(args: str) -> tuple[list[str], bool]:
    """Return (sources, is_from_stage) for COPY/ADD args. JSON-array form is not checked."""
    toks = args.split()
    from_stage = False
    while toks and toks[0].startswith("--"):
        if toks[0].startswith("--from"):
            from_stage = True
        toks.pop(0)
    if not toks or toks[0].startswith("["):
        return [], from_stage
    return toks[:-1], from_stage


def validate(text: str, info: ProjectInfo | None = None) -> ValidationResult:
    instrs, findings = parse(text)
    res = ValidationResult(findings=list(findings))
    add = lambda rule, sev, line, msg: res.findings.append(Finding(rule, sev, line, msg))  # noqa: E731

    froms = [i for i in instrs if i.name == "FROM"]
    if not froms:
        add("E001", ERROR, 1, "no FROM instruction")
        return res
    first_real = next((i for i in instrs if i.name != "ARG"), None)
    if first_real and first_real.name != "FROM":
        add("E001", ERROR, first_real.line, f"{first_real.name} appears before the first FROM")

    stages = _stages(instrs)
    stage_names = set()
    for st in stages:
        m = re.match(r"(?:--\S+\s+)*(\S+)(?:\s+(?i:as)\s+(\S+))?", st[0].args)
        if not m:
            add("E001", ERROR, st[0].line, "malformed FROM")
            continue
        image, alias = m.group(1), m.group(2)
        if alias:
            stage_names.add(alias.lower())
        prior = image.lower() in stage_names and image.lower() != (alias or "").lower()
        if image != "scratch" and "$" not in image and not prior and "@sha256:" not in image:
            tail = image.rsplit("/", 1)[-1]
            if ":" not in tail or tail.endswith(":latest"):
                add("W001", WARN, st[0].line, f"base image '{image}' is unpinned (no tag or :latest); builds are not reproducible")

    project_files = info.files if info else None
    for i in instrs:
        if i.name == "RUN":
            if PIPE_TO_SHELL.search(i.args):
                add("E004", ERROR, i.line, "downloads a script and pipes it to a shell (curl|sh): unverifiable remote code execution at build time")
            if re.search(r"\bapt(-get)?\s+install\b", i.args) and not re.search(r"(\s-y\b|--yes|--assume-yes|\s-qq?y?\b)", i.args):
                add("E012", ERROR, i.line, "apt-get install without -y: build will hang/fail on the confirmation prompt")
            if re.search(r"\bapt(-get)?\s+install\b", i.args) and "rm -rf /var/lib/apt/lists" not in i.args:
                add("W004", WARN, i.line, "apt package lists are not cleaned in the same layer; image is larger than needed")
            if re.search(r"\bsudo\b", i.args):
                add("W005", WARN, i.line, "sudo in a Dockerfile is unnecessary (build already runs as root)")
            if re.search(r"\bpip3?\s+install\b", i.args) and "--no-cache-dir" not in i.args and "PIP_NO_CACHE_DIR" not in text:
                add("W006", WARN, i.line, "pip install without --no-cache-dir keeps the download cache in the layer")
        elif i.name in ("ENV", "ARG"):
            for key, val in re.findall(r"(\w+)=(\S+)", i.args) or ([tuple(i.args.split(None, 1))] if i.name == "ENV" and len(i.args.split(None, 1)) == 2 else []):
                if SECRET_NAME.search(key) and val.strip("'\"") and not val.startswith("$"):
                    add("E006", ERROR, i.line, f"{i.name} {key} sets a literal secret-like value: it is baked into the image layers and visible in `docker history`")
        elif i.name in ("COPY", "ADD"):
            if i.name == "ADD" and re.match(r"(--\S+\s+)*https?://", i.args):
                add("W007", WARN, i.line, "ADD from a URL: prefer RUN curl with a checksum, or COPY a vendored file")
            srcs, from_stage = _copy_sources(i.args)
            if project_files is not None and not from_stage and not re.match(r"(--\S+\s+)*https?://", i.args):
                for s in srcs:
                    if not _exists(s, project_files):
                        add("E007", ERROR, i.line, f"{i.name} source '{s}' does not exist in the project (or was excluded as a secret/ignored file)")
        elif i.name == "EXPOSE" and info and info.port:
            ports = [int(p) for p in re.findall(r"\b(\d{1,5})(?:/\w+)?\b", i.args)]
            if ports and info.port not in ports:
                add("W008", WARN, i.line, f"EXPOSE {ports} does not include the detected app port {info.port}")

    # Layer-cache rule: a bare `COPY . .` before dependency installation busts the cache on every code edit.
    seen_copy_all = None
    for i in instrs:
        if i.name == "COPY" and not _copy_sources(i.args)[1]:
            srcs, _ = _copy_sources(i.args)
            if any(s.rstrip("/") in (".", "./") for s in srcs) and seen_copy_all is None:
                seen_copy_all = i.line
        if i.name == "RUN" and INSTALL_DEPS.search(i.args) and seen_copy_all is not None:
            add("W002", WARN, seen_copy_all, "COPY . . comes before dependency installation: every source edit invalidates the dependency layer cache")
            break

    final = stages[-1]
    runtime = [i for i in final if i.name in ("CMD", "ENTRYPOINT")]
    if not runtime:
        add("E011", ERROR, final[0].line, "final stage has no CMD or ENTRYPOINT")
    elif info and info.entrypoint:
        cmd_text = " ".join(r.args for r in runtime)
        name, stem = PurePosixPath(info.entrypoint).name, PurePosixPath(info.entrypoint).stem
        uses_npm = bool(info.start_command and info.start_command.startswith("npm") and "npm" in cmd_text)
        if name not in cmd_text and f"{stem}:" not in cmd_text and not uses_npm:
            add("W003", WARN, runtime[-1].line, f"CMD/ENTRYPOINT does not reference the detected entrypoint '{info.entrypoint}'")
    users = [i for i in final if i.name == "USER"]
    if not users or users[-1].args.split(":")[0].strip() in ("root", "0"):
        add("W009", WARN, (users[-1].line if users else final[0].line), "container runs as root; add a non-root USER")

    res.findings.sort(key=lambda f: (f.line, f.rule))
    return res


def format_findings(res: ValidationResult) -> str:
    if not res.findings:
        return "no findings"
    return "\n".join(f"  {f.severity.upper():5} {f.rule} line {f.line}: {f.message}" for f in res.findings)
