# Progress tracker

| # | Phase | Status |
|---|-------|--------|
| 1 | Environment, Ollama client, `doctor`, tests | done: 19 tests pass on user machine; real-Ollama `ask` run unconfirmed |
| 2 | Inspect a project (stack detection, secret exclusion) | done here: 32 tests pass; 3 sample apps inspected |
| 3 | Generate + validate Dockerfile (deterministic lint) | done here: 66 tests pass, rules mutation-checked; real-model `generate` run pending (user machine) |
| 4 | Build + capture/classify errors | done here: 95 tests, mutation-checked, simulated e2e; REAL docker build not yet run (user machine) |
| 5 | Bounded repair loop | done here: 128 tests, 8 mutations caught, simulated e2e; real model+docker run pending (user machine) |
| 6 | Smoke test + Trivy scan | done here: 165 tests, 8 mutations caught, simulated e2e with real HTTP probe; real Docker run + real Trivy pending (user machine) |
| 7 | Failure injection + evaluation vs baseline | done: offline corpus evaluation, optional real Docker builds, report |
| 8 | Terraform + Azure (optional; only after 1-7 verified) | |

## Verification notes
- Sandbox used by the assistant: Docker CLI present but NO daemon; Ollama not installed;
  PyPI blocked. So Docker/Ollama behaviour must be confirmed on the user's machine.
