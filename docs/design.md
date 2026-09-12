# OrchestraKit Design

## Purpose

OrchestraKit is a portable, project-agnostic configuration compiler for Codex.
Codex is always the root orchestrator: it decomposes work, selects a role,
reviews returned evidence, and owns the final answer or code integration.
Generated workers are leaf executors and must not delegate further.

## User workflow

1. Keep the OrchestraKit repository anywhere on the local machine.
2. Run `bin/orchestra init /absolute/path/to/project` once per project.
3. Edit `.orchestra/project.toml` when changing workflow or profile-to-model
   mappings, and keep any configured project policy files in the project.
4. Run `bin/orchestra sync /absolute/path/to/project` after configuration edits.
5. Run `bin/orchestra doctor /absolute/path/to/project` to verify configuration,
   generated files, and required provider environment variables.
6. Open a new Codex chat with that project as its primary folder. Codex discovers
   the generated project skill, custom agents, and `AGENTS.md` guidance.

## Boundaries

- The kit never changes global Codex configuration.
- The kit never stores credentials. Provider profiles reference environment
  variable names only.
- The kit never overwrites a user-owned custom-agent file or arbitrary
  `AGENTS.md` content.
- Project integration is opt-in and self-contained after synchronization.
- Project names, languages, frameworks, commands, and repository layouts are
  not encoded in the kit.

## Source of truth and generated surfaces

`.orchestra/project.toml` is the orchestration source of truth. Optional
project-owned Markdown policy overlays supply domain rules without coupling the
kit to one repository. A sync compiles these settings into:

- `.codex/agents/orchestra-<role>.toml` — one model-bound Codex custom agent per
  role;
- `.agents/skills/orchestrate-project/SKILL.md` — reusable orchestration policy
  discovered by Codex in every project chat;
- `.agents/skills/orchestrate-project/references/execution-contract.md` — the
  compiled brief schema, review gates, repair limit, evidence rules, and Git
  authority boundaries;
- `.agents/skills/orchestrate-project/agents/openai.yaml` — UI metadata;
- one marked block in `AGENTS.md` — durable instructions that make the root
  Codex use the project skill for substantial, divisible work;
- `.orchestra/manifest.json` — hashes of generated files for drift detection.

All generated files carry an ownership marker. Synchronization refuses to
replace an existing unmarked file at a managed path.

## Configuration model

Profiles separate cost/strength intent from concrete models. Roles select a
profile. A profile may use the normal Codex provider or name a custom provider.

Default profiles:

| Profile | Model | Effort |
|---|---|---|
| `cheap` | `gpt-5.6-luna` | `medium` |
| `balanced` | `gpt-5.6-terra` | `high` |
| `strong` | `gpt-5.6-sol` | `high` |
| `critical` | `gpt-6-astra` | `high` |

Default roles:

| Role | Default profile | Sandbox | Purpose |
|---|---|---|---|
| `explorer` | `cheap` | `read-only` | Locate code and gather evidence |
| `worker` | `balanced` | `workspace-write` | Implement one bounded leaf task |
| `tester` | `balanced` | `workspace-write` | Verify behavior and diagnose failures |
| `reviewer` | `strong` | `read-only` | Review correctness and integration risk |

External Responses-compatible providers are declared under `[providers]` and
selected by setting `provider` on a profile. The compiler places the provider
configuration in the matching custom-agent file, so global Codex configuration
does not need to change.

## Routing policy

The generated skill instructs the root Codex to:

1. Keep small or tightly coupled tasks local.
2. Delegate only independent, substantial work with explicit scope, acceptance
   criteria, sources of truth, and verification.
3. Use `explorer` before implementation when repository evidence is missing.
4. Use `worker` for bounded edits and `tester` for independent verification.
5. Use `reviewer` for risky or cross-cutting changes.
6. Treat every worker result as untrusted evidence until the root checks the
   diff and relevant verification output.
7. Never allow leaf agents to re-delegate.

The active workflow mode controls the review topology:

- `adaptive` delegates only when separation has clear value;
- `strict` requires leaf and final review gates;
- `program` requires leaf, work-package, and final review gates.

Every delegated leaf uses a stable six-part brief. Reviewers are read-only and
independent, return an exact PASS or FAIL, and never fix their own findings.
Failed review can enter only the configured number of repair/re-review cycles.
The repository, Git diff, tests, and task artifacts remain durable truth; chat
memory does not.

## Deliberate omissions

OrchestraKit does not create worktrees, maintain a project management database,
or automatically commit and push. Those concerns depend on repository workflow
and authorization, so they can be layered on later without weakening the
portable model/profile compiler or its execution contract.

## Portability

The runtime uses Python 3.11 standard-library modules only. The shell launcher
resolves the repository root relative to itself, so the kit directory can be
moved. Generated project files contain no absolute path back to the kit.
