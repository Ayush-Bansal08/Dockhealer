# Evaluation protocol (written BEFORE any run)

Purpose: measure what the bounded loop adds over a one-shot generator, and how it behaves on injected faults.
Rule: numbers in any report come only from `dockhealer eval` runs on a real Docker + Ollama machine, with the raw
`results.jsonl` kept. Nothing is estimated, rounded up, or copied from simulated runs.

## Conditions
| name | what runs |
|---|---|
| **one-shot** (baseline) | first attempt of each run: generate -> validate -> build -> smoke. Derived from attempt 1 of the dockhealer run (same model, same prompt, same code path; temperature 0, seed 42), so the comparison is paired. |
| **dockhealer** | same run continued: up to `--max-attempts` model calls (default 3), diagnose + repair between attempts. |
| **reference** | hand-written `baselines/*.Dockerfile`: no model, no repair. Gives a human yardstick for build time, image size, Trivy count. |

## Scenarios (`evaluation/scenarios.json`, fixed before running)
- 3 generation scenarios: flask, fastapi, express examples.
- 6 repairable injected faults (bad base image tag, requirements typo, missing apt package, COPY of a missing file, `npm ci` with no lockfile, app bound to 127.0.0.1 at runtime).
- 1 must-stop fault: a bad dependency pin in the project itself. Correct behaviour = stop, `human_required`, **zero model calls**.
- Trials per scenario: default 3 (more is better; the report shows 95% Wilson intervals so small n looks small).

## Metrics (definition, unit)
1. **One-shot healthy rate** = runs whose attempt 1 built AND passed the smoke test / generation runs.
2. **Healthy with repair rate** = runs ending `success` / generation runs. **Rescued** = healthy with repair but not one-shot.
3. **Model calls to heal** (median, healed runs). 4. **Build duration** (s, median, final successful build; cache-affected, see limits).
5. **Image size** (MB, median). 6. **Smoke pass rate** among runs that reached a smoke test.
7. **Trivy HIGH/CRITICAL count** (median; only with `--scan`). Known CVEs at scan time; never a security verdict.
8. Injection: **fault was real** (attempt 1 not healthy); **classified as expected** (first failure category equals `expect_category`; `VALIDATION` = rejected by the validator before any build); **repaired rate** over repairable faults; **stopped correctly** over must-stop faults (stop_reason `human_required` and 0 model calls).

## Procedure
`ollama serve`; `docker info`; optional `trivy --version`; then
`dockhealer eval --trials 3 --scan --out runs/eval-final`. Keep `results.jsonl`, `meta.json`, `report.md`. Re-render with
`dockhealer eval --report-only runs/eval-final`. Do not edit scenarios or code between seeing results and publishing them;
if you change either, rerun everything and say so.

## Known threats to validity (state them in any write-up)
- Tiny corpus (3 apps); results do not generalise to real repositories.
- Docker layer cache makes later builds faster than the first; run `docker builder prune` first for cold numbers, and say which you did.
- Model nondeterminism and Ollama version/quantisation differences; the model name is recorded in `meta.json`.
- Classifier rules were tuned against the same injected faults (hand-written logs, then real ones): "classified as expected" is partly in-sample.
- A smoke test proves HTTP reachability only.
