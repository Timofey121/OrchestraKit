# Bounded native execution

For source-bound completion, DAG queues, leases, isolated writes, cancellation,
handoff, result contracts and measured profile comparison, read
[reliability](reliability.md) when those operations apply.

Use this runner for a substantial, independent leaf when the root needs an
explicit configured model and effort, actual checks, and bounded repair. Keep
small coupled work in the root. The runner uses the authenticated Codex CLI;
it does not select or replace the current chat's root model.

```text
execute PROJECT --input JOB.json --dry-run
execute PROJECT --input JOB.json --timeout 180
```

The root writes a job with these fields:

```json
{
  "role": "worker",
  "complexity": "simple",
  "risk": "low",
  "size": "substantial",
  "independent": true,
  "brief": {
    "GOAL": "Implement one independently verifiable change.",
    "SOURCES OF TRUTH": ["AGENTS.md", "src/component.py"],
    "SCOPE": "Only src/component.py and tests/test_component.py. Preserve other work.",
    "ACCEPTANCE CRITERIA": "Describe concrete behavior and failure cases.",
    "VERIFICATION": "python3 -m unittest tests.test_component -v",
    "CONTEXT": "Relevant current behavior, constraints, and authority. No commits."
  },
  "checks": [{
    "argv": ["python3", "-m", "unittest", "tests.test_component", "-v"],
    "failure_cause": "implementation"
  }]
}
```

Jobs are per-task data. Profiles, roles, effort, budgets, and policy still come
from `.orchestra/project.toml`. Optional `required_capabilities` is a list of
names. Optional `review_files` lists the exact files covered by a read-only
review; the runner rejects any change to them during execution or checks.
Do not use that field for files an implementation leaf is supposed to edit.

Each check uses an argv list without an implicit shell. The root chooses trusted
checks appropriate to the task. Classify the consequence of a nonzero exit with
`failure_cause`: `implementation`, `capability`, `context`, or `environment`.
Incorrect classification can cause an unnecessary escalation. A missing command,
process failure, or timeout stops execution. Context and environment failures
return to the root. Capability failures also return to the root without launching
a stronger profile until the missing tool or feature is understood. An
implementation failure first receives a repair at the
same profile; a repeated failure can move to the next configured profile.
All repairs and escalations consume `max_repair_cycles`.

The runner serializes shared-checkout jobs. Prepared isolated worktrees can run
within the shared project capacity limit with explicit write paths. The lock does not
control unrelated editors or chats. Every attempt disables delegation, uses a
fresh ephemeral session, sends the bounded brief once, and retains the original
criteria during repair. Native leaves receive a skill-catalog budget from
`context.max_skill_catalog_tokens` (default 1000, maximum 10000). This limits
catalog overhead; it does not limit all input tokens. Supply needed domain
guidance in the brief when catalog truncation could hide it. The root chat's
catalog setting remains unchanged. CLI stdout, stderr, and check artifacts live under a
unique `.orchestra/executions/UUID/` directory, with a durable `receipt.json` and atomic `run.json` stage records. A running
record left by an interrupted parent is evidence for a restart decision; it
does not authorize automatic replay of side effects.
Config or configured policy changes invalidate a run. The timeout applies to
each subprocess and accepts 1 to 3600 seconds. Native execution currently needs
a POSIX host with advisory file locking.

`verified` means a completed leaf response and passing declared checks. The root
still inspects diffs, acceptance criteria, and configured independent reviews
before accepting the overall task. Nonempty prose alone cannot pass: the
process must exit zero and emit `turn.completed` with a final response.

Receipts label model and effort as requested launch settings. Usage is reported
only when supplied by the CLI; missing counters remain unknown. Include all
attempts and root work when measuring a task. The runner supports configured Codex profiles and custom Responses-compatible
providers on POSIX hosts. Custom credentials are read from the environment or
the macOS Keychain and passed only to the child process. Known credential values
are redacted before stdout, stderr and check output are retained.
Use normal host permissions and report a blocked launch instead of bypassing
the sandbox or changing accounts.


Set optional `[workflow].max_observed_tokens` to bound automatic retries using
observed input plus output across all attempts, including cached input. Before
another repair, reaching the threshold stops the runner. Missing, invalid, or
incomplete counters stop that retry when the limit is enabled; they never count
as zero. The first request and a successfully verified result may exceed the
threshold. This does not enforce a hard provider token or money quota. Receipts
record the limit, observed total, coverage, and retry decision.
