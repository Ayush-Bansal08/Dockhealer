# Dockhealer

## Local AI-assisted Dockerfile generation and self-repair

Dockhealer is a local-first DevOps tool that turns a source-code project into a validated,
buildable, and smoke-tested Docker image.

It uses a local Ollama model to generate and repair Dockerfiles, but all important control-flow
decisions are made by deterministic Python code. The model proposes Dockerfiles; Dockhealer decides
whether a proposal is valid, whether a build failure is safe to repair, whether a retry is allowed,
and when a human must take over.

```text
Inspect -> Generate -> Validate -> Build -> Diagnose -> Repair
                                      |                  |
                                      +-> Smoke test <----+
                                      |
                                      +-> Trivy scan -> Report
```

> **Project status:** Phases 1-7 of the local pipeline are complete. Terraform/Azure deployment
> is an optional future phase and is not required for local use.

---

## Why Dockhealer?

Most AI Dockerfile examples stop after printing text that looks like a Dockerfile. That approach
does not prove that:

- the Dockerfile matches the real project,
- copied files exist,
- dependencies can be installed,
- the base image exists,
- the image starts,
- the application listens on the expected port, or
- the generated image has been checked for known vulnerabilities.

Dockhealer treats model output as an **untrusted candidate**. Every candidate passes through
deterministic validation and, when appropriate, a real Docker build. If the build fails, the
failure is classified before the model is asked for a focused repair.

### Core properties

- **Local and privacy-conscious:** project context is sent to a local Ollama server by default.
- **Project-aware:** detects Python/Node projects, frameworks, dependencies, entrypoints, ports,
  and start commands.
- **Deterministic gate:** Dockerfiles are validated before Docker is invoked.
- **Bounded self-repair:** model calls and transient retries have explicit limits.
- **Runtime-aware:** a successful build is followed by an optional hardened smoke test.
- **Security-conscious:** secret-looking files are excluded and sensitive text is redacted.
- **Auditable:** every attempt and result can be written to JSON.
- **Evaluated:** known-good baselines and deliberately broken Dockerfiles test the validator and
  diagnosis system.
- **No paid API required:** the default workflow uses Python, Docker, Ollama, and optionally Trivy.

---

## Quick start

### Requirements

- Python 3.11 or newer
- Docker CLI and a running Docker daemon
- Ollama
- A code-capable Ollama model
- Trivy is optional and required only for vulnerability scanning

Docker and Ollama are not required to run the unit tests or the offline evaluation.

### 1. Install Dockhealer

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

On Windows/WSL, activate the environment using the activation command appropriate for your shell.

### 2. Prepare Ollama

Install Ollama using the official Ollama instructions, start the server, and pull a model:

```bash
ollama serve
ollama pull qwen2.5-coder:3b
```

`ollama serve` may already be running as a system service. Do not start a second server if one is
already listening on port `11434`.

The default model is `qwen2.5-coder:3b`. You can override it with `DOCKHEALER_MODEL`.

### 3. Run the preflight check

```bash
dockhealer doctor
```

The doctor checks:

- Python version
- available RAM
- free disk space
- Docker CLI and daemon
- Ollama reachability
- whether the configured model is installed

For machine-readable output:

```bash
dockhealer doctor --json
```

### 4. Run the complete workflow

```bash
dockhealer heal examples/flask_app \
  --out /tmp/flask-output \
  --scan
```

If successful, the final Dockerfile is written to:

```text
/tmp/flask-output/Dockerfile
```

The run report and one Dockerfile per attempt are written under a timestamped directory in
`runs/`, for example:

```text
runs/20261023-094423-flask_app/report.json
runs/20261023-094423-flask_app/Dockerfile.attempt1
runs/20261023-094423-flask_app/Dockerfile.attempt2
```

The `--scan` option enables an optional Trivy scan. If Trivy is not installed, the heal continues
and records that the scan was skipped.

---

## The main commands

### `dockhealer heal`: the actual product workflow

Use `heal` when you want Dockhealer to work on a project:

```bash
dockhealer heal PATH [OPTIONS]
```

Example:

```bash
dockhealer heal examples/fastapi_app \
  --out /tmp/fastapi-output \
  --max-attempts 3 \
  --max-retries 1 \
  --scan
```

The command performs:

1. Project inspection
2. Initial Dockerfile generation
3. Deterministic validation
4. Staged-context Docker build
5. Build-log diagnosis
6. Targeted repair, when the failure is repairable
7. A bounded retry or repair loop
8. Container smoke testing
9. Optional Trivy scanning
10. JSON report writing

Important options:

| Option | Meaning |
|---|---|
| `--out DIR` | Write the final Dockerfile to `DIR/Dockerfile` only after success |
| `--force` | Allow overwriting an existing Dockerfile in `--out` |
| `--max-attempts N` | Maximum number of model calls, from 1 to 10 |
| `--max-retries N` | Maximum unchanged rebuilds after transient failures, from 0 to 3 |
| `--timeout SECONDS` | Timeout for each Docker build |
| `--keep-image` | Keep successfully built images instead of removing them |
| `--no-smoke` | Skip the container smoke test |
| `--scan` | Run an optional Trivy vulnerability scan |
| `--report-dir DIR` | Choose where `report.json` and attempt files are written |

Exit codes:

| Code | Meaning |
|---:|---|
| `0` | The image built and passed the enabled completion checks |
| `1` | Unsupported project or general command-level input problem |
| `2` | Environment, model, or file-access problem |
| `3` | Dockerfile failed deterministic validation |
| `4` | Build, smoke test, or bounded healing did not succeed |

### `dockhealer evaluate`: test Dockhealer itself

`evaluate` is a developer/regression command. It does not generate a Dockerfile for a new project.
It tests the repository's known-good and deliberately broken Dockerfiles:

```bash
dockhealer evaluate --report runs/evaluation.json
```

The default mode is offline and checks validation behavior. To also run valid cases through Docker:

```bash
dockhealer evaluate \
  --build \
  --timeout 600 \
  --report runs/evaluation.json
```

The expected result is:

```text
EVALUATION: PASS (with Docker builds)
```

The distinction is:

| Command | Purpose |
|---|---|
| `dockhealer heal PROJECT` | Normal user workflow |
| `dockhealer evaluate` | Regression test for Dockhealer's own logic |

### `dockhealer inspect`: see what the model will receive

```bash
dockhealer inspect examples/flask_app
dockhealer inspect examples/flask_app --context
dockhealer inspect examples/flask_app --json
```

This reports the detected:

- language
- framework
- dependency file
- dependencies
- entrypoint
- runtime version
- application port
- start command
- included files
- excluded files
- warnings

`--context` prints the redacted context that will be included in the generation prompt. Review this
before using Dockhealer on a sensitive project.

### `dockhealer generate`: generate and validate only

```bash
dockhealer generate examples/flask_app
dockhealer generate examples/flask_app --out /tmp/generated
dockhealer generate examples/flask_app --out /tmp/generated --force
```

This command asks the model for a Dockerfile and validates the result, but does not build an image.
The generated file is written only when validation succeeds.

### `dockhealer validate`: deterministic Dockerfile linting

```bash
dockhealer validate \
  baselines/flask_app.Dockerfile \
  --project examples/flask_app
```

This command does not call Ollama and does not run Docker. It is useful for fast local checks and
CI preflight.

### `dockhealer build`: build an existing Dockerfile

```bash
dockhealer build examples/flask_app \
  --dockerfile baselines/flask_app.Dockerfile
```

The Dockerfile is validated first. Use `--skip-validation` only when intentionally testing a
Dockerfile that the validator is expected to reject:

```bash
dockhealer build examples/flask_app \
  --dockerfile failures/flask__bad_base_image.Dockerfile \
  --skip-validation
```

Useful options:

```bash
dockhealer build PROJECT --dockerfile FILE --timeout 600
dockhealer build PROJECT --dockerfile FILE --keep-image
dockhealer build PROJECT --dockerfile FILE --save-log /tmp/build.log
```

Saved logs are redacted before being written.

### `dockhealer smoke`: test an existing image

```bash
dockhealer smoke IMAGE --project examples/flask_app
```

The project tells Dockhealer which application port to publish and probe. The smoke test checks
`/health` and `/`, preferring a successful response but treating any non-5xx HTTP response as
evidence that the server is reachable.

### `dockhealer scan`: scan an existing image

```bash
dockhealer scan IMAGE
dockhealer scan IMAGE --severity HIGH,CRITICAL
dockhealer scan IMAGE --fail-on CRITICAL
```

The scan uses Trivy's vulnerability scanner only. It reports known CVE matches; it does not prove
that an image is secure.

### `dockhealer ask`: test the local model connection

```bash
dockhealer ask "Reply with exactly: OK"
```

This is a small Ollama connectivity and response test.

---

## End-to-end architecture

### 1. Scanner

The scanner in `scanner.py` examines a project without executing its code. It detects supported
Python and Node projects and extracts facts such as:

- Flask, FastAPI, Django, Express, or generic Python/Node stack
- `requirements.txt`, `pyproject.toml`, or `package.json`
- likely entrypoint
- declared or inferred port
- likely start command
- project files relevant to the build context

The scanner intentionally does not blindly send the entire directory to the model.

### 2. Secret exclusion and redaction

The scanner excludes secret-looking paths, including examples such as:

```text
.env
*.pem
*.key
*.p12
*.pfx
*.tfstate
*.tfvars
credentials*
.npmrc
.pypirc
.netrc
```

It also redacts secret-like values in text as defense in depth. Symlinks are not followed, and
there are limits on the number and size of files considered.

This reduces accidental disclosure, but it is not a guarantee that every possible secret format
will be detected. Never use a tool like this as a substitute for secret management or secret
rotation.

### 3. Generation

The local model receives deterministic project facts and a constrained prompt. The prompt asks for:

- a pinned slim official base image
- `/app` as the working directory
- dependency installation before source copying for layer caching
- a non-root runtime user
- the detected port
- an exec-form `CMD`
- binding to `0.0.0.0`
- no invented files
- no secrets in `ENV` or `ARG`
- no `curl | sh` or `wget | sh`

The generator extracts a Dockerfile from a fenced or unfenced model response. A response without a
`FROM` instruction is rejected.

### 4. Validator

The validator is plain Python and does not depend on the model. It produces stable finding IDs:

- `E001`-`E012`: errors that block a build
- `W001`-`W009`: warnings about quality or hardening

Examples of checks include:

- missing or misplaced `FROM`
- invalid Dockerfile instructions
- shell-piped installers
- hardcoded secrets
- missing `COPY` sources
- invalid dependency file references
- missing `CMD` or `ENTRYPOINT`
- unsafe or incomplete package installation patterns
- unpinned base images
- root-user warnings
- weak caching patterns

Errors prevent a build. Warnings are recorded and may be shown in the report.

### 5. Staged Docker build

Dockhealer does not point Docker at the original project directory. Instead it:

1. Creates a temporary build context.
2. Copies only scanner-approved files.
3. Writes the generated Dockerfile outside the user project.
4. Runs Docker with an argument list rather than a shell command.
5. Applies a hard timeout.
6. Captures a bounded build-log tail.
7. Removes temporary files after the build.

This protects the user's project from generated-file writes and reduces the chance of accidentally
baking excluded files into the image.

### 6. Deterministic diagnosis

Build logs are classified by ordered rules. The order matters because one error message can resemble
several categories. For example, a package-resolution message during a network outage should be
classified as a network problem before it is classified as a bad package.

Typical categories include:

| Category | Meaning | Default action |
|---|---|---|
| `NETWORK` | Registry or package download connectivity failure | Retry unchanged Dockerfile |
| `BASE_IMAGE_NOT_FOUND` | Base image or tag does not exist | Repair |
| `MISSING_FILE` | `COPY` or `ADD` refers to an absent file | Repair |
| `LOCKFILE_MISSING` | `npm ci` requires a missing lockfile | Repair |
| `DEPENDENCY_IN_DOCKERFILE` | Dockerfile installation references a bad package | Repair |
| `DEPENDENCY_IN_PROJECT` | Project dependency file is the source of failure | Stop for a human |
| `DOCKER_UNAVAILABLE` | Docker daemon cannot be reached | Stop |
| `DISK_FULL` | Docker storage is full | Stop |
| `TIMEOUT` | Build exceeded its time budget | Stop |
| `UNKNOWN` | No known rule matched | Repair within the remaining budget |

### 7. Bounded repair loop

The healer in `healer.py` owns the control flow:

```text
generate
  |
  +-- invalid ----------------------> repair
  |
  +-- valid -> build
                 |
                 +-- success -> smoke test -> scan -> done
                 |
                 +-- NETWORK -> bounded unchanged retry
                 |
                 +-- repairable failure -> targeted repair
                 |
                 +-- human-only failure -> stop
```

The model never decides whether to continue. The loop stops because:

- `max_attempts` limits model calls,
- `max_retries` limits unchanged transient retries,
- repeated Dockerfiles are detected,
- repeated diagnoses are detected,
- human-only failures stop immediately,
- environment and model errors stop immediately.

Stable stop reasons are recorded in reports:

```text
built
budget_exhausted
human_required
no_progress
repeated_failure
llm_error
environment
```

### 8. Smoke test

After a successful build, the optional smoke test starts the image with:

- `--cap-drop ALL`
- `no-new-privileges`
- 512 MB memory limit
- 1 CPU
- 256-process limit
- no host mounts
- not privileged
- host port published on `127.0.0.1` only

It waits for the container to answer on `/health` and `/`. The container is always removed after
the test.

The smoke test can catch failures that Docker build cannot, such as:

- wrong `CMD`
- missing runtime module
- missing runtime file
- nonexistent runtime user
- incorrect port binding
- application startup crash
- application listening only on `127.0.0.1` inside the container

This is not a perfect sandbox. Docker shares the host kernel; hostile workloads require stronger
isolation such as a disposable VM, gVisor, or Kata Containers.

### 9. Trivy scan

With `--scan`, Dockhealer scans only an image that passed the build and enabled smoke test:

```bash
dockhealer heal examples/flask_app --scan
```

The scanner requests vulnerability results at `HIGH` and `CRITICAL` severity by default. It records:

- total findings
- counts by severity
- findings with available fixes
- a capped list of top findings
- whether Trivy was available

Trivy proves only that known vulnerability records matched packages found in the image at scan time.
It does not prove:

- no zero-day vulnerabilities exist,
- the application code has no bugs,
- secrets are absent,
- configuration is secure,
- every vulnerability is reachable,
- the image is production-ready.

Treat scan output as a triage signal, not a security verdict.

---

## Evaluation and Phase 7

Phase 7 verifies the system against two groups:

### Baselines

Known-good Dockerfiles in [baselines/](/home/ayush_bansal/dockhealer/baselines):

```text
baselines/flask_app.Dockerfile
baselines/fastapi_app.Dockerfile
baselines/express_app.Dockerfile
```

All baselines should pass deterministic validation. A baseline failure indicates a false positive
or a stale reference.

### Failure injections

Deliberately broken Dockerfiles in [failures/](/home/ayush_bansal/dockhealer/failures):

```text
failures/express__npm_ci_no_lockfile.Dockerfile
failures/flask__apt_missing_package.Dockerfile
failures/flask__bad_base_image.Dockerfile
failures/flask__missing_copy_source.Dockerfile
failures/flask__typo_requirements_path.Dockerfile
```

A failure injection is considered detected when:

1. the validator rejects it before Docker, or
2. Docker fails and the diagnosis classifier identifies the failure category.

A failure injection that unexpectedly builds is reported as a regression in the evaluation corpus.

Run the evaluation:

```bash
# Fast, offline validation
dockhealer evaluate --report runs/evaluation.json

# Include real Docker builds
dockhealer evaluate \
  --build \
  --report runs/evaluation.json
```

The evaluation report is JSON and contains the case name, application, validation findings, build
status, and diagnosis category.

The current repository evaluation has verified:

- three baselines validate cleanly,
- the missing-copy case is caught by validation,
- the remaining injected build failures are classified as:
  `LOCKFILE_MISSING`, `DEPENDENCY_IN_DOCKERFILE`, `BASE_IMAGE_NOT_FOUND`, and `MISSING_FILE`,
- the full test suite passes.

This is a small regression corpus. It does not measure the model's success rate over all possible
projects and does not prove that every possible Dockerfile failure is detected.

---

## Example projects

The repository contains small examples used for demonstrations and tests:

### Flask

```bash
dockhealer inspect examples/flask_app
dockhealer heal examples/flask_app --out /tmp/flask-output
```

### FastAPI

```bash
dockhealer inspect examples/fastapi_app
dockhealer heal examples/fastapi_app --out /tmp/fastapi-output
```

### Express

```bash
dockhealer inspect examples/express_app
dockhealer heal examples/express_app --out /tmp/express-output
```

These projects are intentionally small. They are useful for learning and regression testing, not
as production application templates.

---

## Configuration

All configuration is optional:

| Variable | Default | Purpose |
|---|---|---|
| `DOCKHEALER_MODEL` | `qwen2.5-coder:3b` | Ollama model name |
| `DOCKHEALER_OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_HOST` | fallback if `DOCKHEALER_OLLAMA_HOST` is unset | Ollama server URL |
| `DOCKHEALER_TIMEOUT` | `180` | Ollama request timeout in seconds |

Examples:

```bash
export DOCKHEALER_MODEL=qwen2.5-coder:7b
export DOCKHEALER_OLLAMA_HOST=http://127.0.0.1:11434
export DOCKHEALER_TIMEOUT=300

dockhealer doctor
```

The doctor recommends a model based on available RAM:

- 12 GiB or more: `qwen2.5-coder:7b`
- 6-12 GiB: `qwen2.5-coder:3b`
- below 6 GiB: `qwen2.5-coder:1.5b`

These are guidelines. Docker, the operating system, and Ollama all compete for memory.

---

## Reports and artifacts

Every healing run can produce:

```text
runs/<timestamp>-<project>/
  report.json
  Dockerfile.attempt1
  Dockerfile.attempt2
  Dockerfile.attempt3
```

The report includes:

- project path
- overall success
- stop reason
- every candidate Dockerfile
- validation findings
- warnings introduced by repairs
- build status and duration
- image tag and size when available
- diagnosis category and evidence
- smoke-test result
- vulnerability scan result
- model call count
- build count
- total duration

Log-derived fields are redacted and capped before being stored or sent to the model.

Reports are useful for:

- debugging a failed run,
- comparing models,
- measuring repair attempts,
- reviewing why a human handoff occurred,
- creating future regression fixtures.

Do not commit reports that contain sensitive project information.

---

## Development

Install development dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Run the full test suite:

```bash
pytest -v
```

The tests are designed to run without Ollama or a Docker daemon. External processes are replaced
with injected fakes where appropriate.

Run only a focused test module:

```bash
pytest -q tests/test_evaluate.py
pytest -q tests/test_healer.py
pytest -q tests/test_smoke_and_scan.py
```

Run syntax compilation:

```bash
python -m compileall -q src tests
```

Run the repository evaluation:

```bash
dockhealer evaluate
dockhealer evaluate --build
```

### Testing design

The project uses dependency injection for external boundaries:

- Docker build runner
- Docker cleanup runner
- HTTP probe
- sleep and clock functions
- Ollama client behavior
- scanner behavior

This allows failure paths to be tested deterministically without requiring a live external service.
Build-log fixtures in [tests/fixtures/](/home/ayush_bansal/dockhealer/tests/fixtures) verify the
diagnosis categories and rule ordering.

---

## Repository layout

```text
.
├── baselines/
│   ├── express_app.Dockerfile
│   ├── fastapi_app.Dockerfile
│   └── flask_app.Dockerfile
├── failures/
│   ├── express__npm_ci_no_lockfile.Dockerfile
│   ├── flask__apt_missing_package.Dockerfile
│   ├── flask__bad_base_image.Dockerfile
│   ├── flask__missing_copy_source.Dockerfile
│   └── flask__typo_requirements_path.Dockerfile
├── examples/
│   ├── express_app/
│   ├── fastapi_app/
│   └── flask_app/
├── docs/
│   ├── LEARNING_RECORD.md
│   ├── PHASE4_LESSONS.md
│   ├── PHASE5_LESSONS.md
│   ├── PHASE6_LESSONS.md
│   ├── PHASE7_LESSONS.md
│   └── PROGRESS.md
├── src/dockhealer/
│   ├── builder.py
│   ├── cli.py
│   ├── config.py
│   ├── diagnose.py
│   ├── doctor.py
│   ├── evaluate.py
│   ├── generator.py
│   ├── healer.py
│   ├── llm.py
│   ├── repair.py
│   ├── scanner.py
│   ├── smoke.py
│   ├── validator.py
│   └── vulnscan.py
├── tests/
├── pyproject.toml
└── README.md
```

### Module responsibilities

| Module | Responsibility |
|---|---|
| `config.py` | Environment-based model, host, timeout, and sampling settings |
| `llm.py` | Ollama HTTP client and typed model errors |
| `doctor.py` | Preflight checks and RAM-based model recommendation |
| `scanner.py` | Project inspection, safe file selection, and redaction |
| `generator.py` | Generation prompt, Dockerfile extraction, and safe writing |
| `validator.py` | Deterministic Dockerfile rules |
| `builder.py` | Staged-context Docker builds, timeouts, and cleanup |
| `diagnose.py` | Ordered build-log classification and repair/retry/stop policy |
| `repair.py` | Targeted repair prompts using untrusted, bounded logs |
| `smoke.py` | Restricted container startup and HTTP probing |
| `vulnscan.py` | Optional Trivy integration and result parsing |
| `healer.py` | Bounded end-to-end feedback loop and run reports |
| `evaluate.py` | Baseline and failure-injection evaluation |
| `cli.py` | Command-line interface |

---

## Troubleshooting

### `docker CLI not found`

Install Docker and ensure it is on `PATH`:

```bash
docker version
```

On WSL2, enable Docker Desktop integration for the active Linux distribution.

### Docker daemon is unreachable

Start Docker Desktop or the Docker service, then run:

```bash
docker version
dockhealer doctor
```

### Ollama is unreachable

Check the server:

```bash
curl http://localhost:11434/api/version
```

Then check the configured host:

```bash
echo "$DOCKHEALER_OLLAMA_HOST"
dockhealer doctor
```

### The model is not installed

Check the configured model:

```bash
echo "$DOCKHEALER_MODEL"
ollama list
ollama pull qwen2.5-coder:3b
```

### The model is too slow or runs out of memory

Use a smaller model:

```bash
export DOCKHEALER_MODEL=qwen2.5-coder:1.5b
```

Reduce competing Docker/WSL memory usage and check the recommendation from:

```bash
dockhealer doctor
```

### The validator rejects the generated Dockerfile

Run generation separately to inspect the findings:

```bash
dockhealer generate examples/flask_app
```

Then validate a saved Dockerfile:

```bash
dockhealer validate /path/to/Dockerfile --project examples/flask_app
```

The validator is intentionally conservative. An error must be fixed before the Docker build.

### The build fails repeatedly

Inspect the report:

```bash
find runs -name report.json -print
```

Common outcomes:

- `DEPENDENCY_IN_PROJECT`: fix the project's dependency file; the model is not allowed to edit it.
- `DOCKER_UNAVAILABLE`: fix Docker or permissions.
- `NETWORK`: check connectivity; only a small unchanged retry budget is allowed.
- `HUMAN_REQUIRED`: inspect the final diagnosis and report.
- `REPEATED_FAILURE`: the model produced the same failure again.

Increase the model attempt budget only when appropriate:

```bash
dockhealer heal examples/flask_app --max-attempts 5
```

More attempts do not guarantee a better result and increase runtime and model usage.

### Trivy is skipped

This is expected when Trivy is not installed. The build and smoke-test result remain independent
from the optional scan:

```text
vulnerability scan: trivy is not installed: scan skipped
```

Install Trivy using its official documentation, then run:

```bash
dockhealer scan IMAGE --fail-on CRITICAL
```

---

## Security model and limitations

Dockhealer applies multiple safety boundaries:

1. Project files are scanned without executing project code.
2. Symlinks are not followed.
3. Secret-looking files are excluded.
4. Context files and logs are capped.
5. Sensitive values are redacted.
6. Generated Dockerfiles are validated before building.
7. Docker commands use argument arrays, not shell interpolation.
8. Builds run in temporary staged contexts.
9. Model-provided Dockerfiles cannot directly modify the user's project.
10. Runtime smoke tests use reduced container privileges and localhost-only port publishing.
11. Repair and retry budgets are finite.

These measures reduce risk; they do not make arbitrary untrusted code safe. In particular:

- Docker is not a perfect security sandbox.
- The model can still generate a poor or unsafe Dockerfile that passes incomplete rules.
- Secret detection is heuristic.
- A Docker build can execute commands from a Dockerfile.
- A smoke test does not prove application correctness.
- Trivy does not detect every security problem.
- The model may send project context to the local Ollama process; secure that process and host.

For hostile workloads, use a disposable VM or stronger container isolation and review Dockerfiles
before building.

---

## What the project proves

The project demonstrates a complete local feedback loop:

```text
probabilistic generation
        +
deterministic validation
        +
real build feedback
        +
bounded repair
        +
runtime verification
        =
auditable Dockerfile automation
```

It demonstrates that known failure cases can be detected and classified, and that model-driven
automation can be bounded by ordinary program logic.

## What the project does not prove

A successful run does not prove that:

- the application is functionally correct,
- all endpoints work,
- the image is free of vulnerabilities,
- the image is optimized,
- the image is production-ready,
- every secret has been removed,
- every possible Docker error can be diagnosed,
- the model will succeed on every framework or repository.

Treat the result as an assistive starting point that still deserves engineering review.

---

## Learning material

The implementation is accompanied by phase-specific lessons:

- [Progress tracker](docs/PROGRESS.md)
- [Phase 4: build and diagnosis](docs/PHASE4_LESSONS.md)
- [Phase 5: bounded repair loop](docs/PHASE5_LESSONS.md)
- [Phase 6: smoke testing and Trivy](docs/PHASE6_LESSONS.md)
- [Phase 7: failure injection and evaluation](docs/PHASE7_LESSONS.md)
- [Learning record](docs/LEARNING_RECORD.md)

---

## License

Add the license that applies to your repository before publishing a public release. No license is
assumed by this README.
