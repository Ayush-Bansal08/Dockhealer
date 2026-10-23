"""Central configuration. Everything is overridable via environment variables so that
experiments are reproducible and nothing is hard-coded (the original repo hard-coded the model)."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5-coder:3b"


def _normalize_host(raw: str) -> str:
    """Ollama's own OLLAMA_HOST is often written as '127.0.0.1:11434' with no scheme."""
    raw = raw.strip().rstrip("/")
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    return raw


@dataclass(frozen=True)
class Settings:
    ollama_host: str = DEFAULT_HOST
    model: str = DEFAULT_MODEL
    # Read timeout in seconds. Generous because the first call must load the model into RAM
    # on a CPU-only machine; a hung server must still not block us forever.
    request_timeout_s: float = 180.0
    # Determinism: temperature 0 + fixed seed => same prompt gives (nearly) the same output.
    # Reproducible experiments need this.
    temperature: float = 0.0
    seed: int = 42

    @classmethod
    def from_env(cls) -> "Settings":
        host = os.environ.get("DOCKHEALER_OLLAMA_HOST") or os.environ.get("OLLAMA_HOST") or DEFAULT_HOST
        return cls(
            ollama_host=_normalize_host(host),
            model=os.environ.get("DOCKHEALER_MODEL", DEFAULT_MODEL),
            request_timeout_s=float(os.environ.get("DOCKHEALER_TIMEOUT", cls.request_timeout_s)),
        )
