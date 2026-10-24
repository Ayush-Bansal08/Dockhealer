# Interview Q&A (say only what you have verified)

**What does it do?** Local tool: inspect project, local LLM drafts a Dockerfile, deterministic validator + real build + container smoke test judge it, failures are classified and fed back for bounded repair, with a report.
**Why not trust the model?** Output is probabilistic; every decision about continuing, stopping, or accepting is plain code with hard budgets.
**Security?** Secrets excluded/redacted from model context; logs treated as untrusted data (prompt-injection defence); model can only change the Dockerfile; no model-run host commands; containers run with dropped caps, limits, loopback port. Docker is not a perfect sandbox; Trivy shows known CVEs only.
**How do you know it helps?** Fixed scenarios and metrics (docs/EVAL_PROTOCOL.md), paired one-shot vs repair, Wilson intervals. Then quote YOUR measured numbers and the limits (tiny corpus, author-written faults).
**What stops it?** Budgets, repeated failure, no progress, human-only diagnoses (e.g. a dependency pin that does not exist).
**What would you do next?** Real log fixtures, bigger corpus, Terraform/Azure deployment (optional).
