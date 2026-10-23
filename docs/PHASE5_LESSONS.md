# Phase 5: the bounded repair loop

Goal: when the first Dockerfile fails, fix it automatically, but **never loop forever, never guess when a
human is needed, and never trust the model's output without re-checking it.**

## The loop (`healer.heal`)
```
generate -> validate --(errors)--> repair --+
              | ok                          |
              v                             |
            build --(fail)--> diagnose -----+--> REPAIR: ask model (budget!)
              | ok                |--> RETRY : same file again (small budget)
              v                   '--> STOP  : human needed
            BUILT
```
The model only supplies candidate Dockerfiles. **Whether to continue is decided by plain code.**

## Why it always terminates
Two finite budgets: `max_attempts` (model calls, default 3, CLI-capped at 10) and `max_retries`
(unchanged rebuilds after a network failure, default 1, capped at 3). Every loop pass returns or spends one.
So builds <= max_attempts + max_retries. `HardBound` tests this for every setting with an adversarial model.

## Six stop reasons (`healer.py`)
| Reason | Meaning |
|---|---|
| `built` | success |
| `budget_exhausted` | used all model attempts |
| `human_required` | diagnosis says STOP (e.g. a bad package in YOUR requirements.txt, Docker down, disk full, timeout) or network failed after the retry |
| `no_progress` | the model returned a Dockerfile it already tried (whitespace changes don't count) |
| `repeated_failure` | the same failure (category + failing step) came back after a repair |
| `llm_error` / `environment` | model server or Docker unusable |

## Design points to defend
- **Validate before building**: an invalid Dockerfile costs milliseconds, not a docker build.
- **Targeted repair prompt** (`repair.py`): facts + current Dockerfile + one precise problem; "change only what is needed".
- **Logs are untrusted input** (prompt injection via package output): redacted, truncated, fenced as data. And even if
  the model were fooled, its only output is a Dockerfile that must pass the validator and build in a staged context.
- **The model can only edit the Dockerfile**, never your source: a failing project dependency is a STOP, not a repair.
- **New warnings are recorded** (`Attempt.new_warnings`): a "fix" that quietly adds `runs as root` shows up in the report.
- **Every run writes `report.json`** + one Dockerfile per attempt: this is the data Phase 7's evaluation uses.

## Verified here (simulated model + docker) vs NOT verified
- Verified: 128 tests; 8 mutations each broke a test (budget, no-progress, STOP, both repeated-failure guards, retry
  budget, validator gate, log redaction); end-to-end `dockhealer heal`: bad draft (python:3.99) -> BASE_IMAGE_NOT_FOUND
  -> repair -> built; and a project-dependency failure stopped after 1 model call.
- NOT verified: how often a real local model produces a correct repair, and real Docker log wording. That is what you measure.

## Run on your machine (needs Ollama + Docker running)
`dockhealer heal examples/flask_app --out /tmp/flask_out` : runs the whole loop, prints each attempt, writes
`runs/<time>-flask_app/report.json`, and writes `/tmp/flask_out/Dockerfile` ONLY if the image built
(never overwrites an existing Dockerfile without `--force`). Exit 0 healed, 4 not healed.
To force a repair, temporarily edit the prompt or use a weaker model (`DOCKHEALER_MODEL=llama3.2:1b`) and compare reports.

## Exercises
1. Why is `max_attempts` counted in model calls and not in loop iterations?
2. A repair deletes the failing `RUN pip install` line, and the build passes. Which of our checks would catch that? Which would not?
3. `UNKNOWN` failures get action REPAIR. Argue for STOP instead, then say what data would settle it.

## Interview questions
- "How do you prevent an LLM-driven retry loop from running away?"
- "When should the system refuse to repair?"
- "What happens if the build log contains a prompt-injection string?"
