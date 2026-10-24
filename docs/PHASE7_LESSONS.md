# Phase 7: failure injection and evaluation

## Why
A demo that works once proves little. Two questions matter: (1) does the loop beat one-shot generation, (2) does it stop when it should. Both need fixed scenarios, defined metrics, and honest arithmetic.

## What was built
- `heal(initial_dockerfile=...)`: start from a given Dockerfile (an injected fault, or a hand-written reference with `max_attempts=0`). Attempt kind `provided`; no model call unless a repair is needed.
- `failures/` + `evaluation/scenarios.json`: 3 generation scenarios, 6 repairable faults (5 build-time, 1 runtime: `flask run` binds 127.0.0.1), 1 must-stop (project's own pin `flask==99.0.0`, where a repair would be wrong). One fault (`missing COPY source`) is caught by the validator (E007) before any build: the manifest says so.
- `evaluate.py`: runs scenarios through `heal`, one row per run, aggregates (one-shot vs repair, repair rate, classification accuracy, correct stops), Wilson 95% intervals, report with provenance and limits. It computes; it never invents.
- `dockhealer eval` (`--trials`, `--only`, `--scan`, `--no-smoke`, `--report-only DIR`).

## Key ideas
- **Paired baseline**: one-shot = attempt 1 of the same run (same prompt, temp 0, seed 42), so no extra model time and no unfair comparison.
- **Protocol before data**: metrics and rules were written first (`docs/EVAL_PROTOCOL.md`); the manifest must not be edited after seeing results.
- **Small n**: 3 trials x few scenarios gives wide intervals (3/3 has lower bound 44%). Report k/n, not "100% reliable".
- **Correct stop counts only if no model call was made** (a stop that burned a repair call is not a correct stop).
- **Bias to disclose**: the author wrote both faults and classifier rules.

## Verified here (183 tests, mutation-checked)
Manifest integrity; every reference validates clean; each injected fault passes the validator except the one declared as validator-caught; Wilson values; end-to-end `run_eval` through the real `heal` with a fake Docker and scripted model (reference, repair rescue, classification, must-stop with zero model calls); report provenance; `--report-only`; `heal(initial_dockerfile)` budgets. Mutations caught: wrong interval, one-shot always healthy, ignored initial Dockerfile, stop counted despite model call. One surviving mutant is equivalent (`outcome == "built"` already implies smoke passed/skipped).

## NOT verified (needs your machine)
Real Docker log wording for each fault, whether `qwen2.5-coder:3b` repairs them, all numbers. Do not quote anything until `dockhealer eval` has produced `results.jsonl` on your machine.

## Your run
```
dockhealer doctor
dockhealer eval --trials 3 --scan --label "my laptop, qwen2.5-coder:3b"
```
Expect a long run (about 10 scenarios x 3 trials, each with builds). Keep `runs/eval-*/results.jsonl`; paste `report.md` back and capture real failing logs as fixtures.

## Exercises
1. Why is one-shot read from attempt 1 rather than a separate run? What does it assume about determinism?
2. Add an injected fault the classifier does not know. What should the expected category be, and why is adding it after seeing results a problem?
3. Why must `human_required` scenarios assert zero model calls?
