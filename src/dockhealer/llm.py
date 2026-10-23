"""Thin, typed client for the Ollama HTTP API.

Why raw HTTP instead of the `ollama` package? The package is a wrapper around exactly these
calls. Using `requests` directly lets us (a) set explicit timeouts, (b) map failures to typed
errors the repair loop can reason about, and (c) explain the protocol in an interview.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

from .config import Settings

CONNECT_TIMEOUT_S = 3.0  # fail fast if nothing is listening on the port


class LLMError(Exception):
    """Base class for all model-communication failures."""


class LLMUnavailable(LLMError):
    """Ollama server is not reachable (not started, wrong host/port)."""


class ModelNotFound(LLMError):
    """Server is up but the requested model has not been pulled."""


class LLMTimeout(LLMError):
    """Server accepted the request but did not answer within the read timeout."""


@dataclass(frozen=True)
class ChatResult:
    content: str
    model: str
    duration_s: float  # server-side total time; useful later for the evaluation report
    output_tokens: int


class OllamaClient:
    def __init__(self, settings: Settings | None = None, session: requests.Session | None = None):
        self.settings = settings or Settings.from_env()
        self._http = session or requests.Session()

    def _url(self, path: str) -> str:
        return f"{self.settings.ollama_host}{path}"

    def version(self) -> str:
        """GET /api/version - cheapest 'is the server alive?' probe."""
        try:
            r = self._http.get(self._url("/api/version"), timeout=(CONNECT_TIMEOUT_S, 5))
            r.raise_for_status()
            return str(r.json().get("version", "unknown"))
        except requests.ConnectionError as e:
            raise LLMUnavailable(f"Cannot reach Ollama at {self.settings.ollama_host}") from e
        except (requests.RequestException, ValueError) as e:
            raise LLMError(f"Unexpected response from Ollama version endpoint: {e}") from e

    def list_models(self) -> list[str]:
        """GET /api/tags - names of locally pulled models."""
        try:
            r = self._http.get(self._url("/api/tags"), timeout=(CONNECT_TIMEOUT_S, 10))
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
        except requests.ConnectionError as e:
            raise LLMUnavailable(f"Cannot reach Ollama at {self.settings.ollama_host}") from e
        except (requests.RequestException, ValueError, KeyError) as e:
            raise LLMError(f"Unexpected response from Ollama tags endpoint: {e}") from e

    def chat(self, prompt: str, system: str | None = None, model: str | None = None) -> ChatResult:
        """POST /api/chat with streaming off, deterministic sampling, and a hard timeout."""
        model = model or self.settings.model
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": self.settings.temperature, "seed": self.settings.seed},
        }
        try:
            r = self._http.post(
                self._url("/api/chat"),
                json=payload,
                timeout=(CONNECT_TIMEOUT_S, self.settings.request_timeout_s),
            )
        except requests.ConnectionError as e:
            raise LLMUnavailable(f"Cannot reach Ollama at {self.settings.ollama_host}") from e
        except requests.Timeout as e:
            raise LLMTimeout(f"No answer within {self.settings.request_timeout_s:.0f}s") from e
        except requests.RequestException as e:
            raise LLMError(str(e)) from e

        if r.status_code == 404:
            raise ModelNotFound(f"Model '{model}' not found. Run: ollama pull {model}")
        if r.status_code != 200:
            raise LLMError(f"Ollama returned HTTP {r.status_code}: {r.text[:200]}")

        try:
            body = r.json()
            content = body["message"]["content"]
        except (ValueError, KeyError, TypeError) as e:
            raise LLMError("Malformed response from Ollama (missing message.content)") from e

        return ChatResult(
            content=content,
            model=body.get("model", model),
            duration_s=body.get("total_duration", 0) / 1e9,  # Ollama reports nanoseconds
            output_tokens=int(body.get("eval_count", 0)),
        )
