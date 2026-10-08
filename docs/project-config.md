# Project configuration

Each connected project owns one source file: `.orchestra/project.toml`.

## Orchestration

```toml
version = 1
name = "My Project"

[orchestration]
max_parallel = 3
default_profile = "strong"
```

`max_parallel` must be from 1 through 8. It is a policy limit recorded in the
generated skill; the Codex host can impose a stricter runtime limit.
`default_profile` documents the preferred strength for root-level planning, but
the actual root-chat model remains the model selected in Codex.

## Workflow

```toml
[workflow]
mode = "adaptive"
max_repair_cycles = 2
fresh_worker_per_leaf = true
fresh_reviewer_always = true
review_levels = ["final"]
checkpoint_policy = "manual"
git_publication = "human-controlled"
```

`mode` is one of:

- `adaptive`: small and tightly coupled work stays with the root; substantial
  independent tracks may be delegated;
- `strict`: every implementation leaf requires an independent leaf review and
  the integrated result requires final review;
- `program`: adds work-package review between leaf and final review for large
  structured efforts.

`max_observed_tokens` is an optional positive integer in `[workflow]`. It
counts observed input plus output from all supplied attempt logs, including
cached input. Before another automatic retry, reaching the threshold stops
execution; incomplete or invalid counters also stop that retry. The first call
and a successfully verified result can exceed the threshold. This is a retry
boundary, not a hard provider quota or billing estimate. Omit the setting to
retain only the repair-cycle limit.

`max_repair_cycles` is from 0 through 5. `fresh_worker_per_leaf` prevents context
from one new leaf leaking into another. `fresh_reviewer_always` gives each
review and re-review a clean independent context.

Optional `require_fresh_evidence` is boolean and defaults to false for legacy
compatibility. Set it to true to require source-bound `evidence_ref` fields on
required completion checks and reviews and an explicit source snapshot before
queue claims. Artifact and source mutations invalidate those bindings.

Optional `max_launches` is a positive integer. It limits attempts within a native
job and dispatches across the local queue's persisted history. Queue observations
and reserved estimates also use `max_observed_tokens` before dispatch; incomplete
settled expense stops new dispatches while that limit is enabled. This is local
admission policy, not a hard provider or billing quota. See the skill's
[operational contract](../skills/orchestra/references/reliability.md) for schemas,
leases, isolation and acceptance gates.

`review_levels` accepts `leaf`, `work-package`, and `final`. Strict mode requires
`leaf` and `final`; program mode requires all three. `checkpoint_policy` is
`manual` or `after-review`, but never grants permission to commit by itself.
`git_publication` is `human-controlled` or `never`; neither permits automatic
pushes, pull requests, or deployments.

Projects created before OrchestraKit 0.2 remain valid. Missing workflow fields
receive the defaults shown above.

## Project context

```toml
[context]
policy_files = [
  "docs/product-rules.md",
  "docs/testing-policy.md",
]
max_brief_chars = 12000
max_result_chars = 6000
max_skill_catalog_tokens = 1000
```

Policy files are optional, project-relative Markdown files. They must already
exist when `sync` or `doctor` runs. They are applied in listed order; later
files may narrow earlier ones but cannot override higher-priority instructions
or a nearer applicable `AGENTS.md`. OrchestraKit validates but never edits them.

Both character limits must be integers from 1000 through 1000000. Oversized
briefs and result records are refused; constraints are never silently truncated.
These limits measure characters, not tokens. Detailed logs can live in task
artifacts referenced by compact evidence.

## Routing

```toml
[routing]
enabled = true
profile_order = ["cheap", "balanced", "strong", "critical"]
```

`enabled` defaults to true. `profile_order` lists every configured profile once,
from least to most capable. With no explicit order, canonical names use the
order above and other names follow their configuration order. Set an explicit
order for custom profile names.

The root supplies complexity (`simple`, `standard`, `complex`, `critical`) and
risk (`low`, `medium`, `high`, `critical`). The higher tier wins; fewer profiles
clamp to the strongest available tier. A reviewer never drops below its role's
profile. A selected profile supplies both model and exact reasoning effort.
Required capabilities filter custom providers. If no enabled compatible profile
exists, the route asks the root to keep the task or report the capability gap.

Disabled routing keeps each role's fixed profile and generates no variants.
Built-in provider models require a host availability check. Routing does not
change the active root chat's model or launch agents itself.

On failure, use `--failure` with `context`, `implementation`, `capability`, or
`environment`, plus `--previous-profile` and `--attempt`. Attempt zero means the
first failed verification; increment it for each subsequent failed repair.
Context failures gather evidence, first implementation failures repair, and
repeated implementation failures may escalate. Capability failures return to
the root to diagnose unavailable tools or host features. Environment failures
and exhausted repair budgets stop execution. The root retains all required checks
and review gates regardless of the chosen tier.

## Profiles

```toml
[profiles.cheap]
model = "gpt-5.6-luna"
effort = "medium"

[profiles.strong]
model = "gpt-5.6-sol"
effort = "high"
```

Every profile requires `model` and `effort`. Add `provider` only for a custom
provider. Profile names are stable intent labels; model identifiers may differ
between projects.

Supported effort values are `none`, `minimal`, `low`, `medium`, `high`, `xhigh`,
`max`, and `ultra`. The selected model must support the configured value.

## Roles

```toml
[roles.worker]
description = "Implementation worker for one bounded leaf task."
profile = "balanced"
sandbox = "workspace-write"
prompt = "worker.md"
```

Every role becomes a project custom agent named `orchestra_<role>`. A role must
reference an existing profile and one bundled role prompt. Supported sandboxes
are `read-only` and `workspace-write`.

The bundled prompts define four general roles: `explorer`, `worker`, `tester`,
and `reviewer`. A project can remove a role or bind it to another profile.

Enabled routing also generates `orchestra_<role>__<profile>` for each other
profile. Variants keep the role's instructions and sandbox. The `route` command
returns the exact identity, including the default name when appropriate.

## Providers

```toml
[providers.example]
name = "Example Provider"
base_url = "https://api.example.com/"
env_key = "EXAMPLE_API_KEY"
wire_api = "responses"
model_catalog_json = ".orchestra/providers/example-models.json"
supports_standalone_web_search = false
capabilities = ["function", "apply_patch"]
```

Provider names and environment-variable names are stored, but secret values are
not. `wire_api` must be `responses`. `model_catalog_json` is optional for Codex
providers whose model metadata is already known; otherwise keep the catalog in
the project so the integration remains portable. `capabilities` tells the root
orchestrator which provider-specific tools it may rely on when routing work.

Select the provider from a profile:

```toml
[profiles.cheap]
model = "provider-model-id"
effort = "high"
provider = "example"
```

Run `orchestra sync` after changes and `orchestra doctor` before relying on the
new profile in a Codex chat.
