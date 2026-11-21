# Durable task state

New projects set `workflow.require_fresh_evidence = true`. Existing project
settings and task history are preserved. `task show` reports saved task status
separately from verification freshness. See [reliability](reliability.md) for
the evidence and handoff formats.

Use the launcher with an explicit PROJECT and task UUID:

```text
task start PROJECT --goal "Concrete outcome and acceptance criteria"
task list PROJECT --limit 20
task show PROJECT TASK_ID
task record PROJECT TASK_ID --input RESULT.json --revision 0
```

`start` creates `.orchestra/runs/UUID.json` and returns compact state. Each chat
keeps its own UUID. `show` returns the current revision; pass that exact revision
to `record`. A stale revision or active write lock refuses the update. Reload
and reconcile changes rather than overwriting another chat. These files are
runtime history, not another project configuration. No implicit current-task
pointer exists. Do not store credentials, full chat transcripts, or huge logs.

A result file contains:

```json
{
  "status": "completed",
  "summary": "Implemented and verified the requested change",
  "completed_steps": ["Implementation", "Verification", "Final review"],
  "next_steps": [],
  "changed_files": ["src/example.py"],
  "checks": [{"command": "REPOSITORY_CHECK_COMMAND", "outcome": "pass", "evidence": "ACTUAL_OUTPUT_OR_ARTIFACT", "required": true, "evidence_ref": ".orchestra/evidence/CHECK_UUID.json"}],
  "acceptance": [{"criterion": "USER_ACCEPTANCE_CRITERION", "satisfied": true, "evidence": "OBSERVED_BEHAVIOR"}],
  "reviews": [{"level": "final", "verdict": "PASS", "evidence": "INDEPENDENT_REVIEW_RESULT", "evidence_ref": ".orchestra/evidence/REVIEW_UUID.json"}],
  "metrics": {"input_tokens": null, "output_tokens": null, "elapsed_seconds": null, "cost_usd": null},
  "failure_cause": null
}
```

Replace placeholders with observed evidence. For ongoing work set status to
`running`; for an actual blocker use `blocked`, a concrete summary and next
steps. Failure causes: `context`, `implementation`, `capability`, `environment`.
Check outcomes: `pass`, `fail`, `skipped`. Review verdicts: `PASS`, `FAIL`; levels:
`leaf`, `work-package`, `final`. Records for all required reviews must be present.
For multiple leaves or packages, include a separate identified review record
for each in the evidence and verify coverage in the root.

When a local check and leaf review pass but a user decision still blocks
overall acceptance, retain both results. Record `status: blocked`, the open
criterion with `satisfied: false` and the reason in `evidence`. Put the
concrete question and dependent verification in `next_steps`. On the answer,
reconcile the original criteria and record progress at the current revision.
Only record final review and completion when the whole requested outcome meets
its confirmed criteria.

### Complete a task with current evidence

Before recording `completed`, reconcile the confirmed criteria with actual
outputs. With `require_fresh_evidence = true`, verify every required check and
every review included in the result. Each needs a current `evidence_ref` for
the relevant project files, including all `changed_files`. Keep separate
certificates for leaf and final review and confirm reviewer independence.

- Run a missing required check with `evidence run PROJECT --input CHECKS.json`.
- After inspecting current evidence from a previous run, adopt it for the
  current task with
  `evidence bind PROJECT --ref REF --kind check|review --task-id UUID`.
  Use the returned reference. Binding preserves the source and artifact
  hashes, and the root establishes reviewer independence.
- Use `evidence check` with the relevant paths to verify coverage. If the files
  or coverage have changed, run the affected checks again and obtain fresh
  review evidence for the updated scope.

While evidence is missing, record `running` or `blocked` and put the concrete
verification step in `next_steps`. After recording, reread `task show` for the same
UUID. With fresh evidence required, success means `status: completed` and
`verification_state: current`. A historical completion can read as `stale` or
`unbound` while keeping its saved status.

Completion refuses missing/failed required checks, unmet acceptance criteria,
missing/failed configured reviews, pending next steps, or an unresolved failure.
State validation checks the report's structure; it does not execute tests or
prove evidence truthful. The root must inspect actual outputs and diffs.

Metrics describe this record's attempt, not a cumulative total. Totals remain
unknown when any attempt lacks a metric. Completed tasks cannot be reopened;
start a new UUID for new scope. A run file keeps the latest result and cumulative
metrics, plus recent result history, within 256 KiB. When it grows past that
limit, the oldest history entries are removed; `history_dropped` counts them.
The revision and cumulative metrics still cover all accepted updates. If the
current state alone exceeds the limit, admission refuses the update without
changing the saved file. Link detailed artifacts instead of embedding large
reports. This byte limit also applies when context character budgets are raised.

## Decisions for continuation

An existing `handoff` may add `decisions` ({text, reason}), `verified_facts`
({text, reason, evidence_ref}) and `rejected_approaches`
({text, reason, artifact_ref}). Keep statements concise. The CLI verifies current
source-bound passing check evidence for facts and safe, retained attempt artifacts
for rejected approaches. Evidence does not establish a statement's semantic
meaning; the root must inspect it. Reconsider a rejected approach when new facts
warrant it. These records never extend authority or replace constraints.

Accepted-solution memory requires root-owned evidence for the task UUID. Bind
prior evidence before completion using the procedure above. Original certificates
remain intact, and check evidence cannot be promoted to PASS review.
