# Durable coordination and evidence

Use these controls for substantial configured work. Small coupled tasks stay in
the root. All settings come from `.orchestra/project.toml`; jobs, graphs,
receipts and handoffs are runtime data.

## Checks and completion

Supply `evidence_files` in implementation jobs. A reviewer may use `review_files`
to pin its subjects throughout execution. Check artifacts and the review report
are hashed with the exact subjects. Reviews begin with `VERDICT: PASS` or
`VERDICT: FAIL`. Evidence is local verification material; the root still checks
scope, actual outputs and reviewer independence.

```text
evidence run PROJECT --input CHECKS.json
evidence check PROJECT --ref .orchestra/evidence/UUID.json --path src/app.py
```

`CHECKS.json` has `files` and `checks`; checks use the execution job's argv and
failure-cause schema. Add an `evidence_ref` to each required check and review in
the task result. With `workflow.require_fresh_evidence = true`, completion refuses
missing refs, stale subjects and changed artifacts. After integration, rerun
checks and obtain fresh review evidence in the parent checkout. Worktree evidence
cannot certify the integrated parent result. Historical completed records retain
their status; `task show` reports `verification_state: stale` when their bindings
no longer match, and `unbound` for legacy records.

## Queue and leases

```text
queue submit PROJECT --input GRAPH.json
queue claim PROJECT GRAPH_ID --owner OWNER --lease-seconds 3600
queue heartbeat PROJECT GRAPH_ID NODE_ID --owner OWNER --lease-token TOKEN
queue inspect PROJECT GRAPH_ID
queue settle PROJECT GRAPH_ID NODE_ID --owner OWNER --lease-token TOKEN --input SETTLEMENT.json
queue cancel PROJECT GRAPH_ID NODE_ID
queue recover PROJECT GRAPH_ID NODE_ID --input RECOVERY.json
```

A graph has `graph_id` and `nodes`. Each node has `id`, `depends_on`, `job` and
`acceptance_refs` (the exact root acceptance criterion strings). Optional fields
are `estimated_tokens`, `source_snapshot` and `input_blocked`. Supply an explicit
fingerprint snapshot for fresh-evidence projects. Cycles, missing dependencies,
duplicate IDs and oversized data are refused. Only nodes whose required parents
are verified become ready. Stale sources and input requirements block dispatch.

Claims reserve project capacity and a fenced lease in one SQLite transaction.
Admission rechecks the project configuration and refuses a changed policy before
committing; callers reload the current configuration before retrying.
The queue is local to one project and shared across chats. Expired running leases
retain capacity. Recovery requires root-supplied `execution_absent`,
`descendants_absent`, `authorized` booleans all true and a `disposition` of
`blocked`, `cancelled` or `retry`. These assertions need actual process evidence;
the database does not discover processes or authorize replay. Unknown prior
expense remains unknown after a retry. Cancellation of running work requests a
drain; it does not release capacity by itself.

Before `settle` with status `verified`, inspect the leaf result, integrate changes,
run parent checks and obtain configured independent reviews. Settlement has
`status`, `result` (a completed task-result object), `evidence_ref` identifying a
required check, and `observed_tokens` or null. The CLI validates the criterion
coverage and fresh gate refs. The lower Python `Queue.finish` API assumes that
the root has performed this acceptance; it is not a substitute for CLI gates.
Queue commands schedule and record work; they do not run a background daemon.
Keep the lease alive during execution and review, and settle it explicitly.

Optional `workflow.max_launches` caps queue dispatches across its stored history
and native attempts within one job. It does not count hidden provider calls.
Optional `workflow.max_observed_tokens` also checks queue observations and
reserved estimates before dispatch. Estimates and cache hits cannot guarantee a
provider quota. Missing estimates or unknown settled expense stop dispatch when
that limit is enabled. Neither limit retroactively caps a launched request.

## Isolated writes

```text
worktree create PROJECT --ref VERIFIED_REF --destination NEW_ABSOLUTE_PATH
execute PROJECT --input JOB.json --workspace WORKTREE --write-path src/app.py
worktree scope WORKTREE --path src/app.py
```

Check the base ref and whether the task needs uncommitted parent changes before
creating isolation. The helper creates a detached worktree from the supplied
commit and copies current project policy and root `AGENTS.md`. Other uncommitted
parent files are not copied. Each leaf receives explicit write paths. The runner
rejects scope changes, commits and altered copied policy; prepared worktrees
can run within a shared project capacity limit. Shared-checkout jobs serialize.
The declared scope is a result admission check, not a tool permission sandbox.
The role's native sandbox and host permissions still apply.

Worktrees and sidecar ownership records are retained. No automatic merge, commit,
removal or forced cleanup occurs. The root integrates the accepted changes under
the user's authority and verifies the integrated checkout.

## Cancellation, handoff and calibration

`cancel EXECUTION_PROJECT EXECUTION_UUID --reason TEXT` requests native cancellation.
For a worktree job use its `execution_project` from the receipt. Polling drains
the process group before terminal cancellation. POSIX process groups do not
control a process that deliberately starts a separate session or remote work.
Phase events retain task/leaf IDs, attempt, owner, timestamp, evidence ref and
observed usage coverage. An interrupted journal never invents a terminal PASS.
Cancellation acknowledgement and terminal persistence share a lock. A request
accepted before terminal persistence makes the result cancelled; a request after
terminal persistence is refused as already terminal.

`handoff PROJECT --input HANDOFF.json` durably writes a compact record tied to
the current task UUID, revision and goal before continuation. Fields are
`task_id`, `revision`, `goal`, `constraints`, `acceptance`, `unresolved_failures`,
`next_step`, `evidence_refs`, `raw_history_refs`. Bounds reject excess context;
constraints and failures are never silently clipped. This validates local task
identity, not a provider conversation identity or automatic model resume.

`calibrate PROJECT --input SAMPLES.json --profile cheap --profile balanced`
compares identical fixture multisets. Each sample contains `fixture_fingerprint`,
`receipt`, `quality` (`checks_passed`, `acceptance_passed`, `review_verdict`) and
`elapsed_seconds`. Include failures, cancellations and every attempt, including
escalations. Usage remains null when incomplete. Quality and complete observations
precede token comparison. Recommendations do not rewrite config or switch the
root model; use enough representative fixtures before changing routing policy.
Complete observations require consistent input/output totals and cache subsets.
Reports identify the initial launch model for each compared profile and include
the cost of all later attempts in that profile's totals.

Jobs may declare a `result_contract` with `required` field types (`object`,
`array`, `string`, `integer`, `boolean`) and `aliases`. For example,
`{"required":{"repositories":"array"},"aliases":{"cards":"repositories"}}`
renames the known wrapper while preserving all values and extra fields.
Duplicate keys, ambiguous aliases and invalid types stop at the root without a
model repair. Semantic content still needs acceptance checks.
