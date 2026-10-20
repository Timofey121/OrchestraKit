# Bounded native execution

For source-bound completion, DAG queues, leases, isolated writes, cancellation,
handoff, result contracts and measured profile comparison, read
[reliability](reliability.md) when those operations apply.

Use this runner for a substantial, independent leaf when the root needs an
explicit configured model and effort, actual checks, and bounded repair. Keep
small coupled work in the root. The runner uses the authenticated Codex CLI;
it does not select or replace the current chat's root model.

## Choosing an execution tool

Use the host's leaf-agent tools when they provide the required role, model,
effort and sandbox and root can retain the required checks and review evidence.
Use the CLI runner when its configured launch, receipts or recovery controller
are needed. Do not launch a nested CLI merely to reproduce an available host
operation. Neither choice grants permission beyond the host boundary.

| Operation | Execution path |
|---|---|
| Read CI jobs or logs | Available repository connector with the same repository, run and attempt; paginate where needed. Otherwise use the supported CLI with its required permissions. |
| Inspect sources or run local checks | Existing sandbox; keep temporary output in permitted locations. A read-only reviewer reports writable verification to root. |
| Inspect installed Orchestra | `setup --check`. A current `setup` or `install-skill` verifies the bundle and returns without rewriting it. Changed installations still require write access. |
| Check a running UI | Keep the live health check. If loopback is denied, an available authorized browser can read the same endpoint; a PID or an old log alone does not establish health. |
| Install hooks/services, write protected Git metadata or publish | Preserve the required permission check for the specific operation. Stage and inspect changes locally first. |

Do not move a denied command into a background service, proxy or different
wrapper to evade its permission check. A connector's supported read operation
uses that connector's own access policy; it is not a general shell executor.
Record unavailable verification as unavailable rather than replacing real
filesystem, network or service checks with mocks. Tool choice can avoid an
unnecessary sandbox-boundary request, but cannot correct a third-party monitor's
classification of requests that still occur.

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

## Ready task recipes

Choose `bugfix`, `feature`, or `refactor` from the user's requested outcome.
`recipe --describe KIND` returns only that recipe's worker and reviewer steps;
it needs no configured project and makes no model call. Use these steps in the
root for small work. Mixed or unclear tasks use the general workflow.

For a configured native job, `recipe PROJECT --input TASK.json` compiles this
compact task to the usual job JSON:

```json
{
  "recipe": "bugfix",
  "goal": "Fix quoted comma parsing while preserving valid zero amounts.",
  "files": ["src/loader.py", "tests/test_loader.py"],
  "scope": "Edit only these files; preserve the public API. No commits.",
  "acceptance": ["Quoted commas parse correctly", "Valid zero amounts remain accepted"],
  "checks": [{"argv": ["python3", "-m", "unittest", "tests.test_loader", "-v"], "failure_cause": "implementation"}]
}
```

Keep only relevant facts in optional `context`. The compiler retains criteria,
scope and check commands, and never executes them. Include tests and relevant
inputs in `files`, including explicitly named files to create. Defaults keep
work in root (`size: small`, `independent: false`); assess complexity and risk
from evidence rather than treating the default `standard`/`medium` as a finding.
For a substantial independent leaf, explicitly set `size: substantial` and
`independent: true`, then pass the compiled JSON to `workflow` or `execute`.
Optional `task_id` and `required_capabilities` follow the ordinary job contract.

For `bugfix` only, optional `reproduction_checks` uses the same nonempty check
list format as `checks`. Supply assertions for the reported input and nearby
behavior, with expected results from the original contract. `workflow` adds
these commands to the required checks and runs them locally before any writer.
If all checks pass without changing declared inputs, it launches the configured
independent reviewer directly. PASS can finish with `no_change: true`; a blocker
still requires repair and fresh review within the ordinary budgets. Green tests
alone never accept the task. Without explicit reproduction checks, the ordinary
worker/review path remains unchanged. `execute` alone does not use this shortcut.

Commands are project code, not a sandbox for tests. Declare their source, inputs
and dependencies in `evidence_files` / `files`; use temporary paths for scratch
data. Changes to declared inputs or environment/context failures stop the
preflight. Undeclared side effects and incomplete assertions cannot be ruled
out automatically. The host and reviewer must assess their adequacy.

An ordinary six-section job can instead add `"recipe": "refactor"`. The native
runner adds only the selected role's steps, once per prompt and within the brief
budget. Repairs and fresh reviews retain the recipe and original requirements.
Model routing, required checks, evidence, repair limits and review gates remain
project-owned. Recipes do not establish real token savings or model quality.

## Full worker and review cycle

For an independent worker task that needs checks and bounded post-review fixes,
use `workflow PROJECT --input JOB.json --max-launches 6 --dry-run`, then omit
`--dry-run` when the model calls are authorized. Supply explicit `evidence_files`
covering the implementation, tests and relevant inputs. The command builds a
read-only reviewer brief from the original task and final file list. It retains
all original criteria and scope during repair. Small coupled work stays in root.

The shared configured repair budget includes test failures and reviewer FAILs.
Each native leaf gets one attempt; the controller chooses repairs/escalation
from typed check causes, never free-form failure guesses. Missing context,
environment and capability failures return to root. Unknown counters stop new
calls; the launch and observed-token limits cover worker and reviewer phases.
Source/check/report drift cannot preserve a PASS. Retain failed phases in cost
comparisons and report unresolved tasks separately.

Reuse is off by default. Only use `--reuse --environment CONDITIONS.json
--dependencies DEPENDENCIES.json` when root can explicitly enumerate relevant
conditions and dependency files (an explicit empty array is allowed). Never
include credentials. Matching check evidence can avoid a repeated worker call;
`fresh_reviewer_always = true` still requires a new reviewer. Reusing a review
also requires that setting to be false. This does not waive required independent
review levels for integration or completion. Unknown external state requires
fresh execution. Every hit rechecks source, policy and artifact hashes and
retains the originating evidence ref; do not claim its original calls were free.

The receipt is `.orchestra/workflows/UUID.json`. Abrupt interruption retains
`workflow.lock` and the dispatch record for root reconciliation; never remove
them just to replay a possibly completed call. The controller does not measure
root chat, billing or development cost, and does not attest serving models.

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
