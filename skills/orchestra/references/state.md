# Durable task state

For fresh completion bindings and compact identity-bound handoffs, read
[reliability](reliability.md). Required check and review records can include
`evidence_ref`; configured fresh-evidence completion validates those artifacts
and their covered files. Historical task status and current verification
freshness are separate fields in `task show`.

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
  "checks": [{"command": "REPOSITORY_CHECK_COMMAND", "outcome": "pass", "evidence": "ACTUAL_OUTPUT_OR_ARTIFACT", "required": true}],
  "acceptance": [{"criterion": "USER_ACCEPTANCE_CRITERION", "satisfied": true, "evidence": "OBSERVED_BEHAVIOR"}],
  "reviews": [{"level": "final", "verdict": "PASS", "evidence": "INDEPENDENT_REVIEW_RESULT"}],
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

Separate review levels require separate evidence certificates when refs are
provided. The root still checks reviewer independence: different certificate
identifiers cannot prove different people or model sessions. Task admission
rechecks the project configuration before persisting a result.

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

For accepted-solution memory, root-owned evidence must identify the task UUID.
After inspecting independent execution/review evidence, root may explicitly
adopt it with `evidence bind PROJECT --ref REF --kind check|review --task-id UUID`
before recording completion. This rechecks and copies the certificate's current
source, check and report bindings under task ownership; original certificates
remain intact. It cannot promote check evidence to PASS review. Ownership is a
local assertion, not proof of reviewer independence; root must establish that.
