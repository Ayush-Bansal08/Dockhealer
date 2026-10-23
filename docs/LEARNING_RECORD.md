# Learning record (kept up to date by the tutor)

## Taught so far
- Phase 1: HTTP client for Ollama, timeouts, typed exceptions, environment checks (explained briefly; commands were given without full teaching)
- Phase 2: scanner, untrusted input, symlinks, redaction (same)
- Phase 3: validator rules, extraction of a Dockerfile from model text (same)
- Phase 4, Lesson 4.1: running another program from Python (`subprocess`): exit code, stdout/stderr, timeout, list vs shell

## Demonstrated understanding
- (none yet: first exercise pending)

## Still unclear
- (to fill from exercise results)

## Code studied in depth
- doctor.check_docker (the existing subprocess call)

- Phase 4 (4.2-4.5, written up in docs/PHASE4_LESSONS.md): build context/layers, staged context, dependency injection, ordered-rule classifier, repair/retry/stop policy

## Phase 7 completed
- Added `dockhealer evaluate` for baseline and failure-injection regression testing.
- Offline evaluation: 3/3 baselines validate cleanly; 1/5 injected failures is rejected before Docker.
- Real Docker evaluation: remaining 4/4 builds failed as intended and were classified as
  `LOCKFILE_MISSING`, `DEPENDENCY_IN_DOCKERFILE`, `BASE_IMAGE_NOT_FOUND`, and `MISSING_FILE`.
- The evaluation is deliberately not a model-quality or security proof; it is a small regression corpus.

## Next
- Phase 8 is optional Terraform/Azure deployment, only after reviewing the local results and costs.
