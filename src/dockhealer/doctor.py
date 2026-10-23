"""`dockhealer doctor`: preflight checks, so failures are diagnosed before a 5-minute build.

Every external probe has a timeout and runs without a shell (argument list), which is the
same discipline we apply later when running docker build.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .llm import LLMError, LLMUnavailable, OllamaClient

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # ok | warn | fail
    detail: str
    hint: str = ""


def recommend_model(ram_gb: float) -> str:
    """Pick the largest code-capable model that leaves headroom for Docker and the OS.

    Rule of thumb: a Q4-quantized model needs roughly its file size plus 1-2 GB of working
    memory, and Docker Desktop is competing for the same RAM.
    """
    if ram_gb >= 12:
        return "qwen2.5-coder:7b"
    if ram_gb >= 6:
        return "qwen2.5-coder:3b"
    return "qwen2.5-coder:1.5b"


def read_meminfo(path: str = "/proc/meminfo") -> dict[str, float]:
    """Return MemTotal / MemAvailable in GiB from a /proc/meminfo-style file."""
    out: dict[str, float] = {}
    for line in Path(path).read_text().splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            out[key] = int(rest.split()[0]) / (1024 * 1024)  # kB -> GiB
    return out


def check_python() -> CheckResult:
    v = sys.version_info
    ok = v >= (3, 11)
    return CheckResult("python", OK if ok else FAIL, f"{v.major}.{v.minor}.{v.micro}",
                       "" if ok else "Need Python 3.11+")


def check_ram(meminfo_path: str = "/proc/meminfo") -> CheckResult:
    try:
        mem = read_meminfo(meminfo_path)
        total, avail = mem["MemTotal"], mem["MemAvailable"]
    except (OSError, KeyError):
        return CheckResult("ram", WARN, f"cannot read RAM on {platform.system()}",
                           "Run inside WSL2 Ubuntu for an accurate reading")
    detail = f"{total:.1f} GiB total, {avail:.1f} GiB available -> suggested model: {recommend_model(total)}"
    hint = "In WSL2 this is the limit set by .wslconfig, not your Windows RAM." if total < 8 else ""
    return CheckResult("ram", OK if total >= 6 else WARN, detail, hint)


def check_disk(path: str = ".") -> CheckResult:
    free_gb = shutil.disk_usage(path).free / 1024**3
    ok = free_gb >= 10
    return CheckResult("disk", OK if ok else WARN, f"{free_gb:.0f} GiB free",
                       "" if ok else "Models + Docker images need ~10 GiB")


def check_docker() -> CheckResult:
    if not shutil.which("docker"):
        return CheckResult("docker", FAIL, "docker CLI not found",
                           "Enable Docker Desktop > Settings > Resources > WSL integration")
    try:
        p = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                           capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return CheckResult("docker", FAIL, "docker daemon did not answer in 10s", "Is Docker Desktop running?")
    if p.returncode != 0:
        return CheckResult("docker", FAIL, "docker CLI found but daemon unreachable",
                           "Start Docker Desktop and enable WSL integration for your distro")
    return CheckResult("docker", OK, f"daemon {p.stdout.strip()}")


def check_ollama(client: OllamaClient) -> CheckResult:
    try:
        return CheckResult("ollama", OK, f"server {client.version()} at {client.settings.ollama_host}")
    except LLMUnavailable:
        return CheckResult("ollama", FAIL, f"not reachable at {client.settings.ollama_host}",
                           "Install: curl -fsSL https://ollama.com/install.sh | sh ; then: ollama serve")
    except LLMError as e:
        return CheckResult("ollama", FAIL, str(e))


def model_matches(wanted: str, installed: list[str]) -> bool:
    """'qwen2.5-coder' with no tag means ':latest' in Ollama."""
    wanted = wanted if ":" in wanted else wanted + ":latest"
    return wanted in installed


def check_model(client: OllamaClient) -> CheckResult:
    wanted = client.settings.model
    try:
        installed = client.list_models()
    except LLMError:
        return CheckResult("model", WARN, "skipped (Ollama unreachable)")
    if model_matches(wanted, installed):
        return CheckResult("model", OK, f"{wanted} is pulled")
    return CheckResult("model", FAIL, f"{wanted} not pulled (have: {', '.join(installed) or 'none'})",
                       f"Run: ollama pull {wanted}")


def run_all(client: OllamaClient) -> list[CheckResult]:
    return [check_python(), check_ram(), check_disk(), check_docker(),
            check_ollama(client), check_model(client)]
