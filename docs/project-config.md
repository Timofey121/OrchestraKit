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
