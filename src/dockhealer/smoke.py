"""Smoke test: does the image we built actually START and ANSWER on its port?

Three different questions (do not confuse them):
  validator passes -> the file is well-formed and not obviously unsafe
  docker build ok  -> an image exists
  smoke test ok    -> a container from that image starts, stays up, and answers an HTTP request
Only the third catches e.g. a Flask app bound to 127.0.0.1, a wrong CMD path, or a missing module.
It still does NOT prove the application's real logic works.

The container is treated as untrusted code. It runs with: no extra Linux capabilities, no-new-privileges,
memory/CPU/process limits, NO host mounts, NOT privileged, and its port published on 127.0.0.1 only.
It is always removed afterwards, even on errors. Docker isolation is not a perfect sandbox (the kernel is
shared); for stronger isolation you would use gVisor/Kata or a throw-away VM.
"""

from __future__ import annotations

import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Callable

from . import diagnose
from .scanner import ProjectInfo, redact

PROBE_PATHS = ("/health", "/")
STARTUP_TIMEOUT_S = 30.0
LOG_LINES = 50
REPAIR, STOP = diagnose.REPAIR, diagnose.STOP


@dataclass(frozen=True)
class SmokeResult:
    ok: bool
    skipped: bool = False
    status: int | None = None            # HTTP status that proved the app answered
    path: str | None = None
    container_exited: bool = False
    exit_code: int | None = None
    duration_s: float = 0.0
    logs: str = ""                       # already redacted
    detail: str = ""
    statuses: dict = field(default_factory=dict)   # path -> last status seen (None = no answer)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never route localhost via a proxy


def http_status(url: str, timeout: float = 2.0) -> int | None:
    """HTTP status code, or None if nothing answered. 4xx/5xx still count as 'answered'."""
    try:
        with _NO_PROXY_OPENER.open(url, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError, ValueError):
        return None
    except Exception:  # http.client errors such as RemoteDisconnected: the server is not speaking HTTP yet
        return None


def _out(p) -> str:
    return (p.stdout or "") + (p.stderr or "")


def run_smoke(tag: str, info: ProjectInfo, *, startup_timeout_s: float = STARTUP_TIMEOUT_S,
              runner: Callable = subprocess.run, probe: Callable[[str], int | None] = http_status,
              sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
              port_fn: Callable[[], int] = free_port) -> SmokeResult:
    if not info.port:
        return SmokeResult(ok=False, skipped=True, detail="no application port detected: nothing to probe")
    host_port = port_fn()
    name = f"dockhealer-smoke-{uuid.uuid4().hex[:10]}"
    start = clock()
    cmd = ["docker", "run", "-d", "--name", name, "--label", "dockhealer=1",
           "-p", f"127.0.0.1:{host_port}:{info.port}",
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           "--memory", "512m", "--cpus", "1", "--pids-limit", "256", tag]
    statuses: dict = {}
    try:
        started = runner(cmd, capture_output=True, text=True, errors="replace", timeout=60)
        if started.returncode != 0:
            return SmokeResult(False, container_exited=True, exit_code=started.returncode, duration_s=clock() - start,
                               logs=redact(_out(started)[-3000:])[0], detail="docker run failed to start the container")
        while True:
            state = runner(["docker", "inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", name],
                           capture_output=True, text=True, timeout=30)
            running, _, code = (state.stdout.strip().partition(" ") if state.returncode == 0 else ("false", "", ""))
            if running != "true":
                logs = runner(["docker", "logs", "--tail", str(LOG_LINES), name], capture_output=True, text=True,
                              errors="replace", timeout=30)
                return SmokeResult(False, container_exited=True, exit_code=int(code) if code.isdigit() else None,
                                   duration_s=clock() - start, logs=redact(_out(logs))[0],
                                   detail="the container exited before it answered")
            for path in PROBE_PATHS:
                statuses[path] = probe(f"http://127.0.0.1:{host_port}{path}")
            # prefer a real success (2xx/3xx); any other non-5xx answer (e.g. 404) still proves the server is reachable
            for limit in (400, 500):
                best = next(((p, st) for p, st in statuses.items() if st is not None and st < limit), None)
                if best:
                    return SmokeResult(True, status=best[1], path=best[0], duration_s=clock() - start, statuses=dict(statuses))
            if clock() - start >= startup_timeout_s:
                logs = runner(["docker", "logs", "--tail", str(LOG_LINES), name], capture_output=True, text=True,
                              errors="replace", timeout=30)
                return SmokeResult(False, duration_s=clock() - start, logs=redact(_out(logs))[0], statuses=statuses,
                                   detail=f"container is running but gave no usable HTTP answer within {startup_timeout_s:.0f}s")
            sleep(1.0)
    finally:
        try:  # always clean up, whatever happened above
            runner(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError):
            pass


# ---- runtime diagnosis: same idea as diagnose.py, for failures AFTER a successful build -------------
_EXIT_RULES = [
    ("RUNTIME_USER_MISSING", r"unable to find user .*no matching entries in passwd file",
     REPAIR, "The Dockerfile selects a user that does not exist in the base image.",
     "Use a user that exists in the base image, or create the application user before switching to it."),
    ("CMD_NOT_FOUND", r"executable file not found|exec format error|not found in \$PATH|exec: .*: not found",
     REPAIR, "The container's start command does not exist in the image.",
     "Use a CMD whose program is installed in the image (python, node, uvicorn ...), in exec form."),
    ("RUNTIME_MODULE_MISSING", r"ModuleNotFoundError|No module named|ImportError|Cannot find module",
     REPAIR, "The app started but could not import a module.",
     "Make sure dependencies are installed in the final image and WORKDIR/COPY put the code where the CMD expects it."),
    ("RUNTIME_FILE_NOT_FOUND", r"can't open file|No such file or directory|ENOENT",
     REPAIR, "The start command refers to a file that is not in the image.",
     "Check WORKDIR and COPY so the entrypoint file exists at the path used in CMD."),
    ("RUNTIME_PERMISSION", r"Permission denied|EACCES",
     REPAIR, "The non-root user cannot read or write something the app needs.",
     "chown/chmod the needed files before switching to USER, or write only to a directory the user owns."),
    ("PORT_IN_USE", r"Address already in use|EADDRINUSE", STOP,
     "The app could not bind its port.", "Look for a second server process or a hard-coded conflicting port."),
]


def _evidence(logs: str, pattern: re.Pattern | None = None, limit: int = 3) -> list[str]:
    lines = [diagnose.clean_line(l) for l in logs.splitlines() if l.strip()]
    hits = [l for l in lines if pattern and pattern.search(l)]
    return (hits or lines)[-limit:]


def diagnose_smoke(result: SmokeResult, info: ProjectInfo) -> diagnose.Diagnosis | None:
    if result.ok or result.skipped:
        return None
    if result.container_exited:
        for category, regex, action, summary, hint in _EXIT_RULES:
            pat = re.compile(regex, re.I)
            if pat.search(result.logs):
                return diagnose.Diagnosis(category, action, summary, hint, _evidence(result.logs, pat))
        if result.exit_code == 0:
            return diagnose.Diagnosis("EXITED_IMMEDIATELY", REPAIR, "The container started and exited with code 0 (nothing kept running).",
                                      "CMD must start a long-running server process, not a script that finishes.", _evidence(result.logs))
        return diagnose.Diagnosis("STARTUP_CRASH", REPAIR, f"The container crashed on start (exit code {result.exit_code}).",
                                  "Read the evidence and fix the start command or environment.", _evidence(result.logs))
    answered = [s for s in result.statuses.values() if s is not None]
    if answered and all(s >= 500 for s in answered):
        return diagnose.Diagnosis("HTTP_5XX", STOP, f"The app answered but with server errors ({sorted(set(answered))}).",
                                  "This is an application error, not a container-recipe problem; a person must look at the app.",
                                  _evidence(result.logs))
    return diagnose.Diagnosis("UNREACHABLE", REPAIR, "The container is running but nothing answered on the expected port.",
                              f"Make the app listen on 0.0.0.0 (not 127.0.0.1/localhost) on port {info.port}, and make EXPOSE/CMD match.",
                              _evidence(result.logs, re.compile(r"127\.0\.0\.1|localhost|Running on|Listening|listening", re.I)))
