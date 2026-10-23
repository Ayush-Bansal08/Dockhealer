"""Turn a raw build log into a decision: repair it, retry it, or stop and ask a human.

The classifier is deliberately DETERMINISTIC (ordered regex rules, first match wins). The model is
only asked to fix a failure after plain code has decided that fixing it is sensible. Reasons:
  - testable: every category has a fixture log and a test
  - cheap and fast: no model call to understand an error
  - safe: the decision to keep spending attempts never rests on the model's opinion

Rule ORDER is part of the logic. Example: with no network, pip prints "No matching distribution found"
-- which looks like a bad package name. NETWORK therefore has to be checked before DEPENDENCY.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .builder import BuildResult
from .scanner import redact

REPAIR, RETRY, STOP = "repair", "retry", "stop"

DOCKER_UNAVAILABLE = "DOCKER_UNAVAILABLE"
DISK_FULL = "DISK_FULL"
NETWORK = "NETWORK"
DOCKERFILE_SYNTAX = "DOCKERFILE_SYNTAX"
BASE_IMAGE_NOT_FOUND = "BASE_IMAGE_NOT_FOUND"
MISSING_FILE = "MISSING_FILE"
LOCKFILE_MISSING = "LOCKFILE_MISSING"
DEPENDENCY_IN_DOCKERFILE = "DEPENDENCY_IN_DOCKERFILE"
DEPENDENCY_IN_PROJECT = "DEPENDENCY_IN_PROJECT"
COMMAND_NOT_FOUND = "COMMAND_NOT_FOUND"
PERMISSION_DENIED = "PERMISSION_DENIED"
TIMEOUT = "TIMEOUT"
UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Diagnosis:
    category: str
    action: str                 # repair | retry | stop
    summary: str
    hint: str                   # one sentence told to the model when action == repair
    evidence: list[str] = field(default_factory=list)
    failed_step: str | None = None      # the shell command that failed, if BuildKit reported it
    failed_line: int | None = None      # Dockerfile line number, if BuildKit reported it


@dataclass(frozen=True)
class Rule:
    category: str
    pattern: re.Pattern
    action: str
    summary: str
    hint: str


def _r(category, regex, action, summary, hint) -> Rule:
    return Rule(category, re.compile(regex, re.I | re.M), action, summary, hint)


RULES: list[Rule] = [
    _r(DOCKER_UNAVAILABLE,
       r"Cannot connect to the Docker daemon|Is the docker daemon running|error during connect|"
       r"permission denied while trying to connect to the .*docker.*socket",
       STOP, "Docker engine is not reachable", "Start Docker Desktop / enable WSL integration, or add your user to the docker group."),
    _r(DISK_FULL, r"no space left on device", STOP, "Disk is full", "Free disk space (docker system prune) and retry."),
    _r(NETWORK,
       r"Temporary failure in name resolution|Could not resolve host|Name or service not known|no such host|"
       r"TLS handshake timeout|i/o timeout|Connection (?:timed out|reset by peer|refused)|"
       r"Failed to establish a new connection|Network is unreachable|Read timed out",
       RETRY, "Network failure while building", "Check connectivity; the same Dockerfile may succeed on retry."),
    _r(DOCKERFILE_SYNTAX,
       r"dockerfile parse error|unknown instruction|file with no instructions|no build stage in current context|unknown flag",
       REPAIR, "Dockerfile has a syntax error", "Fix the invalid instruction on the reported line."),
    _r(BASE_IMAGE_NOT_FOUND,
       r"failed to resolve source metadata|pull access denied|repository does not exist|manifest unknown|"
       r"manifest for \S+ not found",
       REPAIR, "Base image or tag does not exist", "Use an existing official image tag (e.g. python:3.12-slim, node:20-slim)."),
    _r(MISSING_FILE,
       r"failed to compute cache key|failed to calculate checksum|\"/[^\"]+\": not found|"
       r"Could not open requirements file",
       REPAIR, "COPY/ADD refers to a file that is not in the build context", "Only COPY files that exist in the project's file tree."),
    _r(LOCKFILE_MISSING,
       r"can only install with an existing package-lock\.json",
       REPAIR, "`npm ci` needs a package-lock.json that this project does not have", "Use `npm install --omit=dev` instead of `npm ci`."),
    _r(DEPENDENCY_IN_DOCKERFILE,   # placeholder: refined in _refine_dependency
       r"No matching distribution found|Could not find a version that satisfies|Unable to locate package|"
       r"npm (?:ERR!|error) code E404|ResolutionImpossible|No matching version found",
       REPAIR, "A package could not be found", "Fix or remove the package named in the failing RUN command."),
    _r(COMMAND_NOT_FOUND,
       r"(?:^|\s)(?:/bin/)?(?:ba)?sh: (?:\d+: )?[^\s:]+: (?:command )?not found|exit code: 127",
       REPAIR, "A command used in RUN is not installed in the image", "Install the missing tool first, or use a base image that includes it."),
    _r(PERMISSION_DENIED, r"permission denied|EACCES",
       REPAIR, "A step lacks permission", "Do privileged work (installs, chown) before `USER`, and chown files the app writes to."),
]

_FAILED_STEP = re.compile(r'process "/bin/sh -c (.*?)" did not complete successfully: exit code: (\d+)', re.S)
_DOCKERFILE_LINE = re.compile(r"^\S*ockerfile\S*:(\d+)\s*$", re.M)
_STEP_PREFIX = re.compile(r"^#\d+\s+(?:\d+\.\d+\s+)?")
# A failed install whose package list lives in a project file (-r requirements.txt, bare `npm install`) is the
# PROJECT's problem; editing the Dockerfile cannot fix it.
_PROJECT_LEVEL_INSTALL = re.compile(
    r"(?:-r|--requirement)\s*\S+|\bnpm\s+(?:ci|install)\s*(?:--[\w-]+(?:=\S+)?\s*)*(?:&&|;|$)|"
    r"\bpip3?\s+install\s+(?:--[\w-]+\s+)*\.|\byarn\s+install\b|\bpoetry\s+install\b")


def clean_line(line: str) -> str:
    return _STEP_PREFIX.sub("", line).strip()


def _evidence(rule: Rule, log: str, limit: int = 3) -> list[str]:
    out: list[str] = []
    for line in log.splitlines():
        if rule.pattern.search(line):
            c = clean_line(line)
            if c and c not in out:
                out.append(c)
        if len(out) >= limit:
            break
    return out


def _refine_dependency(rule: Rule, failed_step: str | None, log: str) -> Rule:
    if re.search(r"Unable to locate package", log):                 # apt: always named in the Dockerfile
        return rule
    if failed_step is None or _PROJECT_LEVEL_INSTALL.search(failed_step):
        return Rule(DEPENDENCY_IN_PROJECT, rule.pattern, STOP,
                    "A dependency declared by the project itself cannot be resolved",
                    "Fix the package name/version in requirements.txt or package.json; the Dockerfile cannot fix this.")
    return rule


def classify_log(log: str) -> Diagnosis:
    step_match = None
    for step_match in _FAILED_STEP.finditer(log):
        pass
    failed_step = step_match.group(1).strip() if step_match else None
    line_match = None
    for line_match in _DOCKERFILE_LINE.finditer(log):
        pass
    failed_line = int(line_match.group(1)) if line_match else None

    for rule in RULES:
        if rule.pattern.search(log):
            if rule.category == DEPENDENCY_IN_DOCKERFILE:
                rule = _refine_dependency(rule, failed_step, log)
            return Diagnosis(rule.category, rule.action, rule.summary, rule.hint,
                             _evidence(rule, log), failed_step, failed_line)
    return Diagnosis(UNKNOWN, REPAIR, "Build failed for an unrecognised reason",
                     "Read the error output and fix the failing step.",
                     [clean_line(l) for l in log.splitlines() if l.strip()][-3:], failed_step, failed_line)


def diagnose(result: BuildResult) -> Diagnosis | None:
    """None when the build succeeded."""
    if result.success:
        return None
    if result.timed_out:
        return Diagnosis(TIMEOUT, STOP, "Build exceeded the time limit",
                         "Investigate what is slow (huge download, interactive prompt waiting for input).")
    return classify_log(result.log)


def log_for_model(log: str, max_lines: int = 40) -> str:
    """The slice of a build log that may be sent to the model: last lines, step prefixes removed, secrets redacted."""
    lines = [clean_line(l) for l in log.splitlines() if l.strip()]
    text = "\n".join(lines[-max_lines:])
    return redact(text)[0]
