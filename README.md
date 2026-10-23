# dockhealer

AI-assisted Dockerfile generation with a **bounded feedback loop**:
`Inspect -> Generate -> Validate -> Build -> Diagnose -> Repair -> Test -> Report`

Runs fully locally (Python + Docker + Ollama, no paid APIs, no GPU). Azure/Terraform deployment is an
optional later phase and is never required for local use.

> Status: **Phase 7 of 8** (… failure injection and evaluation). See `docs/PROGRESS.md`.

## Quick start (WSL2 Ubuntu)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Ollama (skip if installed)
curl -fsSL https://ollama.com/install.sh | sh
ollama serve &                      # or it may already run as a service
ollama pull qwen2.5-coder:3b        # size chosen by `dockhealer doctor` from your RAM

dockhealer doctor                   # preflight: RAM, disk, Docker, Ollama, model
dockhealer ask "Reply with exactly: OK"
dockhealer validate baselines/flask_app.Dockerfile --project examples/flask_app
dockhealer generate examples/flask_app            # model writes it, validator judges it
dockhealer build examples/flask_app --dockerfile baselines/flask_app.Dockerfile   # needs Docker running
dockhealer heal examples/flask_app --out /tmp/flask_out --scan   # full loop + smoke test (+ Trivy if installed)
dockhealer smoke <image> --project examples/flask_app   # run an image and probe it
dockhealer scan <image> --fail-on CRITICAL              # Trivy scan (optional)
dockhealer evaluate --report runs/evaluation.json       # offline baseline + failure evaluation
dockhealer evaluate --build --report runs/evaluation.json  # include real Docker builds
dockhealer inspect examples/flask_app --context   # what the model would see
pytest -v                           # or: python -m unittest discover -s tests -v
```

Configuration (all optional): `DOCKHEALER_MODEL`, `DOCKHEALER_OLLAMA_HOST`, `DOCKHEALER_TIMEOUT`.

## Layout

```
src/dockhealer/
  config.py   settings from env (model, host, timeout, deterministic sampling)
  llm.py      Ollama HTTP client with typed errors and hard timeouts
  doctor.py   preflight checks + model recommendation by RAM
  scanner.py  safe project inspection: stack, port, start command, secret exclusion/redaction
  generator.py prompt + Dockerfile extraction + safe write (never overwrites without --force)
  validator.py deterministic rules E001-E012 (errors) / W001-W009 (warnings), no model involved
  builder.py  staged-context `docker build` with timeout, capped log, injected runner
  diagnose.py ordered rules: build log -> category + repair/retry/stop decision
  repair.py   targeted repair prompt (logs fenced as untrusted data)
  smoke.py    hardened `docker run` + HTTP probe + runtime diagnosis
  vulnscan.py optional Trivy scan, honest about what it does not prove
  healer.py   the bounded loop: budgets, stop reasons, run report
  cli.py      `heal`, `doctor`, `inspect`, `validate`, `generate`, `build`, `ask`
failures/     Deliberately broken Dockerfiles (some are caught by validation, the rest by builds)
baselines/    hand-written reference Dockerfiles (evaluation baseline; must validate clean)
examples/     tiny Flask / FastAPI / Express apps (also the evaluation corpus later)
tests/        unit tests (no Ollama or Docker needed)
```

## Phase 7: failure injection and evaluation

Run `dockhealer evaluate` from the repository root. The default mode is offline and checks that all
hand-written baselines validate cleanly and every deliberately broken Dockerfile is caught by the
validator. Add `--build` to build cases that pass validation; those failures are then classified
with the same deterministic diagnosis rules used by `heal`. A failure that builds successfully is
reported as a regression in the corpus.

The experiment is intentionally small: it demonstrates that validation and diagnosis catch known
failure classes, not that every possible Dockerfile failure is covered. Record the JSON report,
Docker/Dockerfile versions, model settings, and whether the run had network access before comparing
results across machines.
