# Routing and context

For empirical profile comparison and pre-dispatch queue budgets, read
[reliability](reliability.md). Compare identical fixture multisets and quality
gates with all attempts counted before changing the single project config.

Choose complexity from the actual work: simple for one understood change,
standard for normal implementation, complex for interacting components or
uncertain design, critical for reasoning that demands the strongest configured
profile. Choose risk separately: low for easy-to-revert local changes, medium
for normal product changes, high for broad regressions or sensitive boundaries,
critical for severe consequences. The higher tier wins. Reviewers retain their
configured minimum profile. Do not confuse confidence with low risk.

Example:

```text
route PROJECT --role worker --complexity complex --risk high --independent
route PROJECT --role worker --failure capability --previous-profile balanced --attempt 0
```

Use `--require-capability` repeatedly for provider requirements such as
`function`, `apply_patch`, or `web_search`. Custom providers must declare these
capabilities in project configuration. Built-in models still require a host
availability check. Set `[routing].enabled = false` to retain fixed role
profiles. `profile_order` defines increasing strength and must list all profiles
exactly once. The configured effort travels with each selected profile.

Verification is light for low risk, focused for medium, full for high or critical;
strict and program workflows require full verification. Final review gates
remain in force even for a cheap profile. Missing context means gather evidence
without a stronger model. First implementation failure receives a repair;
repeated implementation failures may escalate. Capability failures return to
the root to check the unavailable tool or host feature. Environment failures
stop until their prerequisite is resolved. Every repair consumes the same bounded
budget, including escalation.

## Context limits and cache-friendly requests

Keep invariant instructions, tool schemas, and relevant stable policy together
before the varying task brief when a provider supports prefix caching. Reuse
that stable prefix unchanged. Give leaves paths and excerpts rather than whole
chat histories; they may read additional relevant files within scope. Never
discard acceptance criteria, safety constraints, or required checks to fit a
budget. Split the leaf or shorten redundant evidence instead.

`max_brief_chars` defaults to 12000 and `max_result_chars` to 6000. These are
character limits, not token estimates. Return a short summary, changed paths,
checks with outcomes/evidence, remaining work, and failure cause. Put verbose
logs in task artifacts and reference their paths. Record actual token counts,
elapsed time, and cost only if supplied by the runtime; unknown values stay null.

## Asynchronous Batch

Use a provider's real Batch API only for independent, nonurgent work when that
API is available and the user has authorized external submission. Persist its
job ID and pending status in task evidence. Inspect each completed item before
using it. Do not batch interactive dependencies, failures requiring immediate
repair, or approval steps. Parallel local agents are not provider Batch jobs.

OrchestraKit does not submit requests to inference APIs, enable server-side
caching, or guarantee discounts. These policies become actionable only through
an available, authorized provider integration. Normal Codex chats still benefit
from bounded context and independent work, without a claimed billing discount.
