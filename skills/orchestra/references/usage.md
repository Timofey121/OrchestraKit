# Observed usage

Load this reference when the user asks to measure tokens or compare model use.
Reading counters is local and does not issue inference requests.

```text
python3 <skill-directory>/scripts/orchestra.py usage ROOT_SESSION.jsonl LEAF_SESSION.jsonl
```

Supply native Codex session logs for the root and each relevant leaf. The parser
reads numeric counters and session model context; it does not copy message or
tool contents into the report. Repeated paths and response IDs are deduplicated.
Cumulative-only legacy logs use their final event, with a coverage warning.
Ephemeral `codex exec --json` traces can also be supplied: each completed turn
contributes its counters, but the model and response count remain unknown.
Do not mix native and CLI copies of one run in separate input files; CLI turn
events have no response IDs for cross-file deduplication.

Treat `input_tokens` as all input, including `cached_input_tokens`.
`uncached_input_tokens` is their difference. `reasoning_output_tokens` is a
subset of `output_tokens`; do not add it again. Missing counters remain null.
The report contains no estimated price.

Models and efforts come from `turn_context`: requested configuration, not an
attestation of the actual inference model. A child's usage is never assigned
the parent's model. Without the child's context, its model stays unknown.

`complete` means supplied lines were parsed and usage was found, not that every
agent or billable event was captured. Partial logs produce warnings. Compare
equivalent completed tasks with identical prompt, starting files, root model,
effort and host. Record failures and retries too. An automatically selected skill
is behavioral evidence; it does not by itself prove lower token consumption.

For before/after workflow measurements, `compare-workflows` accepts `{variant,
sample}` wrappers around the calibration sample format. Preserve task, source,
criteria and initial requested settings; count all attempts, including failures.
The comparison does not execute models or verify supplied quality judgments.
Use independent checks/review first; incomplete observations cannot support a
savings recommendation. See [reliability](reliability.md) for the command.

## Repeatable coding tasks

`eval list` describes six bundled synthetic Python tasks. `eval prepare --output
NEW_DIRECTORY --variant baseline --repeat 2` creates isolated task projects and
external acceptance graders. Preparation, `eval grade`, `eval report` and
`eval compare BEFORE_DIRECTORY AFTER_DIRECTORY` do not invoke models.

Only `eval run DIRECTORY --max-launches N` invokes configured native workers and
independent reviewers. Agree on the live-run scope first. The limit counts
Codex CLI launches, including stored attempts, not internal inference requests
or tokens. Default preparation disables repairs; `--repair-cycles 1|2` enables
them for separate retry experiments. Test projects use the bundled profile
defaults, not a copy of the user's project configuration.

Preserve failed trials and every role's usage. An interrupted start never
authorizes automatic replay. Report development costs separately when known;
unknown host or root costs stay unknown. A baseline alone shows no savings.
