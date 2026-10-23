# Phase 4: build the image, capture the failure, decide what to do

Goal: take a Dockerfile that passed the validator, run `docker build`, and when it fails turn the raw
log into a decision: **repair**, **retry**, or **stop and ask a human**.

## 4.1 Running another program (`subprocess`)
`docker build` is not Python, so Python must start it as a separate process and read three things it
leaves behind: **stdout** (normal output), **stderr** (diagnostics) and the **exit code** (0 = success).
A failing command does NOT raise an exception: you must check the code yourself. A missing program raises
`FileNotFoundError`; a command that runs too long raises `TimeoutExpired` if you passed `timeout=`.
Always pass arguments as a list: with `shell=True`, input like `hello; echo INJECTED` runs a second command.
Real use: `run_build()` in `src/dockhealer/builder.py`; `check_docker()` in `doctor.py`.

## 4.2 What `docker build` does
- **Build context**: the folder sent to the Docker engine. `COPY` can only see what is inside it.
- **Layers and cache**: each instruction creates a layer; unchanged instructions reuse the cache. That is
  why the dependency file is copied and installed BEFORE `COPY . .` (validator rule W002).
- **`--progress=plain`**: prints every step's full output (BuildKit's default UI hides it).
- Three different questions, three different tools:

| Question | Answered by | Cannot tell you |
|---|---|---|
| Is the file well-formed and safe? | `validator.py` (static, instant) | whether files/packages/images exist |
| Does it build into an image? | `builder.run_build` (Phase 4) | whether the app starts or works |
| Does the container run correctly? | smoke test (Phase 6) | whether it is secure or complete |

## 4.3 `builder.py`: design decisions
1. **Staged context** (`staged_context`): Docker is NOT pointed at your project. Only files in
   `info.files` (the scanner's approved list) are copied to a temp dir, symlinks re-checked, size capped
   (`MAX_CONTEXT_BYTES`). Result: a `COPY . .` cannot bake in `.env`, keys or `.git`, and what the
   validator reasoned about is exactly what Docker sees. (Verified here with a fake docker: a planted
   `.env` and a symlink to `/etc/passwd` never appeared in the context.)
2. **Dockerfile outside the context**: written to its own temp dir, passed with `-f`. Nothing generated is
   written into your project. It is named `Dockerfile` so BuildKit errors say `Dockerfile:5`.
3. **Bounded execution**: list arguments, `timeout`, log kept to the last `LOG_TAIL_CHARS` (errors are at
   the end). On timeout `returncode` is `None` and `timed_out=True`.
4. **Dependency injection**: `run_build(..., runner=subprocess.run, clock=time.monotonic)`. Tests pass a
   fake runner, so no Docker daemon is needed. Also why `_text()` exists: `TimeoutExpired` carries bytes.

## 4.4 `diagnose.py`: a deterministic decision table
`RULES` is an ordered list; **first match wins**, so order is logic. Example: with no network, pip prints
"No matching distribution found" which looks like a bad package name, so `NETWORK` is checked first.
Each rule maps to an action:
- `REPAIR`: the Dockerfile is the likely cause (syntax, base image tag, COPY path, lockfile, apt, missing tool).
- `RETRY`: transient (network).
- `STOP`: outside the Dockerfile's control (Docker down, disk full, timeout, **project** dependency).

`_refine_dependency` is the key judgement: `pip install -r requirements.txt` failing is the PROJECT's
problem (`DEPENDENCY_IN_PROJECT`, STOP); `pip install flaskk` typed in the Dockerfile is ours
(`DEPENDENCY_IN_DOCKERFILE`, REPAIR). DockHealer edits only the Dockerfile, never your source.
`log_for_model()` is the only slice of a log that may reach a model: last lines, prefixes stripped, secrets redacted.

## 4.5 CLI: `dockhealer build PATH --dockerfile FILE`
Flow in `cmd_build`: scan -> read Dockerfile -> **validate (refuse if errors)** -> `run_build` -> diagnose.
Exit codes: 0 built, 1 unsupported project, 2 usage/Docker missing, 3 failed validation, 4 build failed.
`--save-log` writes a redacted log (use it to capture REAL fixtures), `--keep-image` skips cleanup.

## What the tests prove, and what they do not
- Proves: staging excludes secrets/symlinks; timeouts and exit codes are mapped correctly; every category
  has a fixture; rule order handles overlapping symptoms. Mutation-checked: breaking the symlink re-check,
  the timeout flag, a rule pattern, and the staging list each made a test fail.
- Does NOT prove: that the real `docker build` accepts these exact arguments, or that real logs match the
  fixtures. **The fixtures are hand-written approximations**; wording differs between Docker/pip/npm versions.
  The real acceptance test is running on your machine and saving real logs (below).

## Run it on your machine (needs Docker Desktop running + WSL integration on)
1. `dockhealer build examples/flask_app --dockerfile baselines/flask_app.Dockerfile`
   Runs the whole path with a known-good Dockerfile. Expect `BUILD OK  ...s  image dockhealer-flask_app:<n>  ~NN MB`, then the image is removed.
   If it prints a Docker-connection error instead, that is an environment problem (`dockhealer doctor`), not a code bug.
2. `dockhealer build examples/flask_app --dockerfile failures/flask__bad_base_image.Dockerfile --save-log bad_base.log`
   Expect exit 4 and `BASE_IMAGE_NOT_FOUND ... next action: repair`.
3. Repeat step 2 with the other files in `failures/` (each passes the validator but cannot build). Expected categories:
   `typo_requirements_path` -> MISSING_FILE, `apt_missing_package` -> DEPENDENCY_IN_DOCKERFILE,
   `express__npm_ci_no_lockfile` (use `examples/express_app`) -> LOCKFILE_MISSING.
   `flask__missing_copy_source` is caught earlier by the validator (exit 3); add `--skip-validation` to see the build error.
   These are predictions; if a category differs, copy the saved log into `tests/fixtures/` with a `# expect:` header. That is how the classifier improves.

## Exercises (find the weaknesses yourself)
1. `run_build` returns `_text(p.stderr) + _text(p.stdout)`. What information is lost compared with one merged stream, and when would it matter?
2. `classify_log` searches the WHOLE log. A pip retry warning early in a log, followed by an unrelated failure, would be classified as what? How would you fix it, and which test would you write first?
3. `classify_log` returns `UNKNOWN` with action `REPAIR`. Is guessing the right default? What is the cost of each choice?

## Interview questions
- "Why copy files to a temp directory instead of building the project folder directly?"
- "Why is rule order part of the logic in your classifier?"
- "Which build failures does your tool refuse to auto-repair, and why?"
