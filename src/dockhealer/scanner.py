"""Project scanner: inspect an untrusted project directory and extract deterministic facts.

Design principles (all interview-defensible):
  1. The repository is UNTRUSTED input. We never execute anything from it, never follow
     symlinks (they could point at ~/.ssh), and cap file counts and sizes.
  2. Secrets never reach the model. Secret-looking files are excluded by name, and file
     contents that are sent are additionally redacted by pattern (defence in depth).
  3. Facts that can be computed deterministically (language, deps, port, start command) are
     computed here and handed to the model, instead of asking a probabilistic model to guess.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

EXCLUDED_DIRS = {".git", "node_modules", "venv", ".venv", "env", "__pycache__", ".pytest_cache",
                 "dist", "build", ".terraform", ".idea", ".vscode", ".mypy_cache", "target"}

# Matched case-insensitively against the file NAME. Deliberately conservative: a false
# exclusion costs a little context, a false inclusion can leak a credential.
SECRET_FILE_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*", "*.p12", "*.pfx",
                        "*.tfstate", "*.tfstate.*", "*.tfvars", "credentials*", "secrets.*", ".npmrc",
                        ".pypirc", ".netrc")

MAX_FILES = 500
MAX_FILE_BYTES = 200_000

REDACTION_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED AWS KEY ID]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "[REDACTED GITHUB TOKEN]"),
    (re.compile(r"(?i)\b((?:api[_-]?key|secret[_-]?key|secret|token|password|passwd|pwd)\w*\s*[:=]\s*)"
                r"(['\"]?)[^\s'\",;]{4,}\2"),
     r"\1[REDACTED]"),
]

PY_ENTRYPOINTS = ("app.py", "main.py", "server.py", "wsgi.py", "run.py", "manage.py")
NODE_ENTRYPOINTS = ("server.js", "index.js", "app.js", "main.js")
DEFAULT_PORTS = {"flask": 5000, "fastapi": 8000, "django": 8000, "express": 3000}
PORT_REGEXES = [re.compile(p) for p in (
    r"port\s*=\s*(\d{2,5})", r"\.listen\(\s*(\d{2,5})", r"PORT\s*\|\|\s*(\d{2,5})",
    r"--port[ =](\d{2,5})")]


@dataclass
class ProjectInfo:
    root: str
    language: str = "unknown"          # python | node | unknown
    framework: str | None = None
    dependencies: list[str] = field(default_factory=list)
    dependency_file: str | None = None
    entrypoint: str | None = None
    runtime_version: str | None = None  # e.g. node engine constraint
    port: int | None = None
    port_source: str = "unknown"        # detected | default | unknown
    start_command: str | None = None
    files: list[str] = field(default_factory=list)
    excluded: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)
    warnings: list[str] = field(default_factory=list)

    @property
    def supported(self) -> bool:
        return self.language in ("python", "node")


def is_secret_filename(name: str) -> bool:
    lower = name.lower()
    return any(fnmatch.fnmatch(lower, pat) for pat in SECRET_FILE_PATTERNS)


def redact(text: str) -> tuple[str, int]:
    """Replace secret-looking substrings. Returns (clean_text, number_of_redactions)."""
    total = 0
    for pattern, repl in REDACTION_PATTERNS:
        text, n = pattern.subn(repl, text)
        total += n
    return text, total


def _walk(root: Path, info: ProjectInfo) -> None:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root)
        keep = []
        for d in sorted(dirnames):
            rel = str(rel_dir / d)
            if d in EXCLUDED_DIRS:
                info.excluded.append((rel + "/", "excluded directory"))
            elif os.path.islink(os.path.join(dirpath, d)):
                info.excluded.append((rel + "/", "symlink (not followed)"))
            else:
                keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            rel = str(rel_dir / f)
            full = os.path.join(dirpath, f)
            if is_secret_filename(f):
                info.excluded.append((rel, "secret-like filename"))
            elif os.path.islink(full):
                info.excluded.append((rel, "symlink (not followed)"))
            elif len(info.files) >= MAX_FILES:
                info.excluded.append((rel, f"file limit ({MAX_FILES}) reached"))
            else:
                info.files.append(rel)


def _read(root: Path, rel: str) -> str:
    p = root / rel
    if p.stat().st_size > MAX_FILE_BYTES:
        return ""
    return p.read_text(errors="replace")


def _req_names(text: str) -> list[str]:
    names = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http")):
            continue
        name = re.split(r"[\[=<>~!; ]", line, maxsplit=1)[0].strip().lower()
        if name:
            names.append(name)
    return names


def _detect_python(root: Path, info: ProjectInfo) -> bool:
    deps: list[str] = []
    if "requirements.txt" in info.files:
        info.dependency_file = "requirements.txt"
        deps = _req_names(_read(root, "requirements.txt"))
    elif "pyproject.toml" in info.files:
        info.dependency_file = "pyproject.toml"
        try:
            data = tomllib.loads(_read(root, "pyproject.toml"))
            deps = _req_names("\n".join(data.get("project", {}).get("dependencies", [])))
        except tomllib.TOMLDecodeError:
            info.warnings.append("pyproject.toml could not be parsed")
    else:
        return False
    info.language, info.dependencies = "python", deps
    info.framework = next((f for f in ("fastapi", "flask", "django") if f in deps), None)
    info.entrypoint = next((e for e in PY_ENTRYPOINTS if e in info.files), None)
    return True


def _detect_node(root: Path, info: ProjectInfo) -> bool:
    if "package.json" not in info.files:
        return False
    try:
        pkg = json.loads(_read(root, "package.json"))
    except json.JSONDecodeError:
        info.warnings.append("package.json is not valid JSON")
        return False
    info.language, info.dependency_file = "node", "package.json"
    info.dependencies = sorted((pkg.get("dependencies") or {}).keys())
    info.framework = next((f for f in ("express",) if f in info.dependencies), None)
    info.runtime_version = (pkg.get("engines") or {}).get("node")
    main = pkg.get("main")
    info.entrypoint = main if main in info.files else next((e for e in NODE_ENTRYPOINTS if e in info.files), None)
    if (pkg.get("scripts") or {}).get("start"):
        info.start_command = "npm start"
    elif info.entrypoint:
        info.start_command = f"node {info.entrypoint}"
    return True


def _detect_port_and_start(root: Path, info: ProjectInfo) -> None:
    source = _read(root, info.entrypoint) if info.entrypoint else ""
    for rx in PORT_REGEXES:
        m = rx.search(source)
        if m:
            info.port, info.port_source = int(m.group(1)), "detected"
            break
    else:
        if info.framework in DEFAULT_PORTS:
            info.port, info.port_source = DEFAULT_PORTS[info.framework], "default"

    if info.language == "python" and info.entrypoint:
        mod = info.entrypoint[:-3]
        if info.framework == "fastapi":
            m = re.search(r"^(\w+)\s*=\s*FastAPI\(", source, re.M)
            app_var = m.group(1) if m else "app"
            info.start_command = f"uvicorn {mod}:{app_var} --host 0.0.0.0 --port {info.port or 8000}"
        elif info.framework == "flask":
            info.start_command = f"python {info.entrypoint}"
            if "app.run(" in source and not re.search(r"host\s*=\s*['\"]0\.0\.0\.0['\"]", source):
                info.warnings.append(
                    "Flask app.run() without host='0.0.0.0' binds to localhost: the container "
                    "will start but be unreachable from outside.")
        else:
            info.start_command = f"python {info.entrypoint}"
    if info.language == "node" and info.entrypoint:
        if re.search(r"\.listen\(\s*[^)]*?['\"]127\.0\.0\.1['\"]|\.listen\(\s*[^)]*?['\"]localhost['\"]", source):
            info.warnings.append("Node server listens on localhost only: unreachable from outside the container.")


def scan(path: str | os.PathLike) -> ProjectInfo:
    root = Path(path).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"{root} is not a directory")
    info = ProjectInfo(root=str(root))
    _walk(root, info)
    if not (_detect_python(root, info) or _detect_node(root, info)):
        info.warnings.append("No supported project detected (need requirements.txt/pyproject.toml or package.json)")
        return info
    _detect_port_and_start(root, info)
    if not info.entrypoint:
        info.warnings.append("Could not determine an entrypoint file")
    return info


def build_context(info: ProjectInfo, max_chars: int = 6000) -> tuple[str, int]:
    """Assemble the text that is allowed to reach the model. Returns (context, redaction_count)."""
    root = Path(info.root)
    parts = [
        "PROJECT FACTS (computed deterministically, treat as ground truth):",
        f"- language: {info.language}",
        f"- framework: {info.framework}",
        f"- entrypoint: {info.entrypoint}",
        f"- port: {info.port} ({info.port_source})",
        f"- start command: {info.start_command}",
        f"- dependency file: {info.dependency_file}",
        f"- runtime version constraint: {info.runtime_version}",
        "",
        "FILE TREE:",
        *[f"  {f}" for f in info.files[:60]],
    ]
    redactions = 0
    for rel in filter(None, (info.dependency_file, info.entrypoint)):
        clean, n = redact(_read(root, rel))
        redactions += n
        parts += ["", f"--- {rel} ---", clean]
    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[TRUNCATED]"
    return text, redactions
