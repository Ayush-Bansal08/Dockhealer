# Phase 6: does the container actually run, and what known vulnerabilities does it carry?

## Three different questions
| Check | Answers | Cannot tell you |
|---|---|---|
| validator | file is well-formed, no unsafe patterns | whether files/packages/images exist |
| docker build | an image exists | whether it starts |
| **smoke test** (`smoke.py`) | a container starts, stays up, answers HTTP on its port | that the app's real logic is correct |
| **vulnerability scan** (`vulnscan.py`) | known CVEs matched in image packages | that the image is secure |

## Smoke test
- Runs `docker run -d` with: `--cap-drop ALL`, `no-new-privileges`, 512m memory, 1 CPU, 256 pids, **no host mounts, not
  privileged**, port published on **127.0.0.1 only**. Always `docker rm -f` afterwards, even on errors.
  Docker is not a perfect sandbox (shared kernel): for hostile code use gVisor/Kata or a throw-away VM.
- Polls `/health` then `/` (default 30s). Any non-5xx answer = reachable; 2xx/3xx preferred in the report.
- Catches what a build cannot: a Flask app bound to 127.0.0.1, a wrong CMD path, a missing module at startup.
- Runtime failures are diagnosed like build failures (`smoke.diagnose_smoke`): UNREACHABLE, CMD_NOT_FOUND,
  RUNTIME_MODULE_MISSING, RUNTIME_FILE_NOT_FOUND, RUNTIME_PERMISSION, EXITED_IMMEDIATELY, STARTUP_CRASH -> repair;
  PORT_IN_USE and HTTP_5XX (an app bug, not a recipe bug) -> stop for a human.
- In `heal`, a container that builds but fails the smoke test goes through the SAME bounded repair loop.

## Vulnerability scan (Trivy, optional)
- `dockhealer scan IMAGE` / `heal --scan`. Skipped cleanly if Trivy is missing; a scanner failure never fails a heal.
- Scans vulnerabilities only (`--scanners vuln`), default severities HIGH,CRITICAL, run on an image that already passed.
- **Proves**: matches against a CVE database at scan time. **Does not prove**: no zero-days, no bugs in your code,
  no weak configuration, no secrets in files. "0 findings" is never "secure". Findings with no fix are often not
  actionable; many are unreachable in practice, so triage, do not panic. `--fail-on` can gate CI on a threshold.

## Verified here vs NOT
- Verified: 165 tests; 8 mutations each broke a test (capability drop, cleanup, 5xx handling, exit detection, smoke
  gating, scanner isolation, vuln-only, loopback-only port); an end-to-end `heal` where the probe made REAL HTTP
  requests to a local server started by a stand-in `docker`.
- NOT verified: real Docker `run/inspect/logs` behaviour, real container hardening effects (some apps may need
  extra capabilities), real Trivy output (the JSON in tests is hand-written from its documented schema), Trivy install.

## Run on your machine
`dockhealer heal examples/flask_app --out /tmp/flask_out --scan` (smoke is on by default; `--no-smoke` disables).
Install Trivy from its official documentation (search "Trivy installation"); do NOT use a `curl | sh` installer, which
is exactly what our own validator rule E004 rejects. Then `dockhealer scan <image> --fail-on CRITICAL`.
Deliberately break the container to see repair: set `app.run(host="127.0.0.1")` in a COPY of the flask sample.

## Exercises
1. Why publish on 127.0.0.1 and not 0.0.0.0? What could another machine on your Wi-Fi do otherwise?
2. A 404 on `/health` counts as "reachable". When is that too lenient? How would you make the probe configurable?
3. Trivy reports 40 HIGH findings in the base image. List three things you would check before acting.
## Interview questions
- "What does your smoke test prove, and what not?" - "Why not call the image secure when the scan is clean?"
