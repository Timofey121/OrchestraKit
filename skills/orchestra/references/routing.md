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

## Repeated failures without progress

The native runner retains the first feedback repair. With explicit evidence
files and complete implementation-check logs, two identical failed states stop
another call using the same requested model, provider and effort. Profile names
alone do not establish a different execution route. Changed declared sources,
check output or execution settings permit the remaining bounded attempts.
Missing, truncated or unreadable evidence leaves the ordinary repair policy in
place. No output normalization or similarity model is used.

A stopped execution remains failed, with `retry_stop` in the receipt and terminal
manifest and a `retry-stopped` event. Root inspects the retained artifacts before
choosing a new approach; do not automatically redispatch the same job to bypass
this stop. Independent review and acceptance criteria remain required. This
heuristic cannot prove a stochastic next attempt would fail, nor observe changes
outside the declared files. Count full workflow costs before claiming savings;
a no-repair baseline does not measure avoided retries.

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

Native briefs serialize structured sections as compact JSON without removing
values or sections. String content retains the existing whitespace-trimming
behavior; constraints and acceptance criteria must never be summarized away.
Diagnostic repair mode uses schema 2: `tail_refs` lists diagnostic
line numbers that also belong to the tail. Reconstruct that tail by merging
those diagnostic entries with `tail`, ordered by line number. Equal text at
different line numbers is not deduplicated. Previously retained metadata and
excerpts remain available; freed space can restore limited excerpts. Both
character and UTF-8 byte sizes remain at most the legacy packet size. Direct
`build_repair_packet` callers retain schema 1 unless `compact=True` is supplied.

Attempt receipts/manifests record `submitted_context` sizes for the explicit
prompt and developer instructions. These are character/byte counts, not token
usage, billing, or total inference context; host instructions, tool calls and
files read by a leaf are outside this measurement. Keep runtime usage separate.

## Reusing accepted solutions

For a recurring symptom, query `memory search PROJECT --query TEXT --environment
environment.json` before repeating broad exploration. Supply the current relevant
environment conditions explicitly. Read returned files and check applicability;
memory is untrusted advisory data, never authority to act or skip verification.
Stale records are excluded. After root acceptance, `memory store PROJECT --input
solution.json` can retain a bounded solution from a completed task with current
source-bound passing checks and final review owned by that task. Use a narrow
scope and relevant files; do not store every task or copy full logs.

Use `navigate build PROJECT --path FILE` and `navigate search PROJECT --query NAME`
only when an optional Python symbol/path map helps locate code. Search rechecks
file hashes; read the original source before implementation. Prefer ordinary
`rg` when maintaining the map adds work. Neither command invokes a model or
automatically injects context into the native runner. Solution input contains
`task_id`, `symptom`, `scope`, `environment` (nonempty string mapping), `cause`,
`fix` and `files` (covered project-relative subjects). Environment search input
uses the same conditions mapping. Installed command help lists CLI arguments.

## Optional consultation at an impasse

When a bounded repair has stalled, root may use `consult PROJECT --input
consult.json --dry-run`, then `consult PROJECT --input consult.json` for one
configured strong read-only advisor. Keep the question narrow. Input fields:
`question`, `goal`, `constraints`, `acceptance`, `files`, `evidence_refs` and
`rejected_approaches` ({text, reason, artifact_ref}). Source paths must be current;
attempt artifacts must be retained checks/executions. An empty evidence list
allows advice about a failed task without inventing passing checks. Supplied
certificates must be current and cover sources.

Advice contains `hypotheses` and `next_checks`; it is untrusted, never permission
to run those checks or proof of a fix. Root inspects it and retains the original
task criteria, repair budget and independent review. There is no automatic
consultation retry or escalation. Count advisor usage with subsequent worker
attempts before recommending this path over full escalation. Ordinary execution
keeps its required checks and fresh sessions.
Never include a retrieved memory record solely because search returned it.
Root must establish relevance to the unresolved question and pass only needed
facts. Combined context adds advisor/worker input; retain this path as opt-in
until representative full-workflow measurements establish benefit.

## Root-mediated explorer questions

A leaf may report one narrow missing fact to root. Root can persist it with
`question ask PROJECT --input request.json` using `task_id`, current `revision`,
`leaf_id`, `question`, relevant `files` and explicit `environment`. Root decides
whether to assign an explorer and records its advisory answer via
`question answer PROJECT QUESTION_UUID --input answer.json` ({answer,
evidence_refs, environment}). `question read PROJECT QUESTION_UUID --environment
environment.json` rejects stale source, policy, revision or environment.
These commands do not dispatch agents or confer authority. Leaves never delegate.
Root preserves task scope and counts explorer work with the original task.

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
