"""Generate a Dockerfile with the local model, then extract it from the reply.

The model is given deterministic facts from the scanner so it only has to do what it is good at:
assembling a standard recipe. Whatever it returns is untrusted text until validator.py approves it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .llm import ChatResult, OllamaClient
from .scanner import ProjectInfo, build_context

SYSTEM_PROMPT = """You write Dockerfiles. Output ONLY the Dockerfile inside a single ```dockerfile code block. No explanation.

Hard requirements:
- Use a pinned, slim official base image tag (never :latest).
- WORKDIR /app. Copy the dependency file first, install dependencies, THEN copy the rest of the source (layer caching).
- Install Python packages with pip --no-cache-dir. For Node use npm install --omit=dev (there may be no lockfile).
- Create and switch to a non-root user. Node official images provide user `node`; Python images do not,
  so create an app user with `useradd` before `USER`, or use the base image's documented user.
- EXPOSE the port given in the project facts.
- Start the app with the exact start command from the project facts, in exec (JSON array) form, listening on 0.0.0.0.
  The first JSON item must be the real executable (`python`, `node`, `uvicorn`, etc.); do not write `exec` as an item.
- Only COPY files that appear in the file tree. Never invent files. Never put secrets in ENV or ARG.
- Never pipe curl or wget into a shell."""


class GenerationError(Exception):
    """The model replied, but no usable Dockerfile could be extracted."""


@dataclass(frozen=True)
class Generation:
    dockerfile: str
    raw_reply: str
    llm: ChatResult
    redactions: int


_FENCE = re.compile(r"```[ \t]*([\w.+-]*)[ \t]*\n(.*?)```", re.S)
_FROM_LINE = re.compile(r"^\s*FROM\s+\S", re.I | re.M)


def extract_dockerfile(reply: str) -> str | None:
    """Pull a Dockerfile out of free-form model text. Returns None when none is found."""
    for _lang, body in _FENCE.findall(reply):
        if _FROM_LINE.search(body):
            return body.strip() + "\n"
    m = _FROM_LINE.search(reply)  # unfenced reply: start at the first FROM line
    if m:
        return reply[m.start():].replace("```", "").strip() + "\n"
    return None


def build_prompt(info: ProjectInfo) -> tuple[str, int]:
    context, redactions = build_context(info)
    return f"Write the Dockerfile for this project.\n\n{context}", redactions


def generate(client: OllamaClient, info: ProjectInfo) -> Generation:
    prompt, redactions = build_prompt(info)
    result = client.chat(prompt, system=SYSTEM_PROMPT)
    dockerfile = extract_dockerfile(result.content)
    if dockerfile is None:
        raise GenerationError("model reply contained no Dockerfile (no FROM line)")
    return Generation(dockerfile, result.content, result, redactions)


def write_dockerfile(content: str, directory: str | Path, force: bool = False) -> Path:
    """Write <directory>/Dockerfile, refusing to overwrite an existing file unless force=True."""
    target = Path(directory) / "Dockerfile"
    if target.exists() and not force:
        raise FileExistsError(f"{target} already exists (use --force to overwrite)")
    target.write_text(content)
    return target
