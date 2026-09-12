# OrchestraKit

OrchestraKit gives any local Codex project the same reusable orchestration
structure while letting each project choose its own models. Codex always stays
the root orchestrator. Generated role agents are bounded leaf executors for
exploration, implementation, testing, and review.

The kit uses Python 3.11 and has no runtime dependencies.

## Quick start

Initialize any existing project:

```bash
/absolute/path/to/OrchestraKit/bin/orchestra init /absolute/path/to/project --name "My Project"
```

The command creates a project configuration, generated Codex custom agents, a
project orchestration skill, a managed `AGENTS.md` block, and a drift manifest.
It preserves existing `AGENTS.md` content and refuses to replace user-owned
files at managed paths.

Open the project as the primary folder of a new Codex chat. Project instructions
and skills are discovered automatically. For a substantial divisible request,
Codex can select the generated skill implicitly; you can also request it
explicitly:

```text
Use $orchestrate-project for this task.
```

Small and tightly coupled work remains in the root chat. Delegated work returns
to the root Codex for verification and integration.

## Change models

Edit the project file:

```text
<project>/.orchestra/project.toml
```

Profiles separate cost and strength from roles. A typical mapping is:

| Profile | Default model | Default effort |
|---|---|---|
| `cheap` | `gpt-5.6-luna` | `medium` |
| `balanced` | `gpt-5.6-terra` | `high` |
| `strong` | `gpt-5.6-sol` | `high` |
| `critical` | `gpt-6-astra` | `high` |

Roles select profiles, so changing one profile can move several roles to a new
model without rewriting their instructions. The model for the root chat remains
the model selected in Codex; project profiles configure spawned leaf agents.

After any configuration change, regenerate and check the project:

```bash
/absolute/path/to/OrchestraKit/bin/orchestra sync /absolute/path/to/project
/absolute/path/to/OrchestraKit/bin/orchestra doctor /absolute/path/to/project
```

See [project configuration](docs/project-config.md) for the full format.

## Custom providers

Responses-compatible model providers can be declared per project. Credentials
are read from environment variables and never stored in the kit or project.
Provider-specific model catalogs can also remain inside the project.

See [DeepSeek setup](docs/providers/deepseek.md) for a concrete example.

## What gets generated

```text
project/
├── .orchestra/
│   ├── project.toml
│   └── manifest.json
├── .codex/agents/
│   ├── orchestra-explorer.toml
│   ├── orchestra-worker.toml
│   ├── orchestra-tester.toml
│   └── orchestra-reviewer.toml
├── .agents/skills/orchestrate-project/
│   ├── SKILL.md
│   └── agents/openai.yaml
└── AGENTS.md
```

Only files carrying the OrchestraKit ownership marker are regenerated. The
project configuration is never regenerated after initialization.

## Commands

```text
orchestra init [PROJECT] [--name NAME]
orchestra sync [PROJECT]
orchestra doctor [PROJECT]
```

`PROJECT` defaults to the current directory. `doctor` exits nonzero when the
configuration is invalid, a generated file drifted, a required provider key is
missing, or a configured model catalog cannot be found.

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Architecture decisions are recorded in [docs/design.md](docs/design.md).

