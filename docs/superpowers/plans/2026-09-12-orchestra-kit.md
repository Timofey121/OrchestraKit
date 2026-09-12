# OrchestraKit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Build a portable Codex-first orchestration kit that compiles one project TOML file into discoverable project agents, a project skill, durable `AGENTS.md` guidance, and a drift manifest.

**Architecture:** A dependency-free Python CLI reads and validates `.orchestra/project.toml`, renders only OrchestraKit-owned Codex files, and records their hashes. The root Codex remains the sole orchestrator; all generated custom agents are leaf roles with configurable model profiles and optional Responses-compatible providers.

**Tech Stack:** Python 3.11 standard library (`argparse`, `dataclasses`, `hashlib`, `json`, `pathlib`, `tomllib`, `unittest`), POSIX shell launcher, TOML, Markdown.

**Spec:** `docs/design.md`

## Global Constraints

- The kit is project-agnostic and contains no assumptions about any user project.
- Codex is always the root orchestrator; generated agents never delegate.
- Do not modify global Codex configuration.
- Never store provider secrets; store environment-variable names only.
- Use no runtime dependencies outside Python 3.11.
- Refuse to overwrite unmarked user-owned files.
- Generate no absolute path back to the kit.

---

### Task 1: Configuration contracts and validation

**Files:**
- Create: `src/orchestra_kit/__init__.py`
- Create: `src/orchestra_kit/config.py`
- Create: `templates/project.toml`
- Create: `templates/roles/explorer.md`
- Create: `templates/roles/worker.md`
- Create: `templates/roles/tester.md`
- Create: `templates/roles/reviewer.md`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: a project directory containing `.orchestra/project.toml` and the kit root containing `templates/roles/*.md`.
- Produces: `load_config(project_root: Path, kit_root: Path) -> ProjectConfig` and `ConfigError`.

- [x] **Step 1: Write failing configuration tests**

```python
def test_load_config_resolves_role_profile_and_provider(self):
    config = load_config(self.project, self.kit)
    self.assertEqual(config.roles["worker"].profile, "cheap")
    self.assertEqual(config.profiles["cheap"].provider, "deepseek")

def test_load_config_rejects_unknown_role_profile(self):
    with self.assertRaisesRegex(ConfigError, "unknown profile"):
        load_config(self.project, self.kit)
```

- [x] **Step 2: Run tests and verify RED**

Run: `PYTHONPATH=src python3 -m unittest tests.test_config -v`

Expected: FAIL because `orchestra_kit.config` does not exist.

- [x] **Step 3: Implement the configuration dataclasses and validation**

```python
@dataclass(frozen=True)
class ProfileConfig:
    name: str
    model: str
    effort: str
    provider: str | None = None

@dataclass(frozen=True)
class RoleConfig:
    name: str
    description: str
    profile: str
    sandbox: str
    prompt: str
```

Parse TOML with `tomllib`, require version `1`, validate names, efforts,
sandbox values, role-to-profile references, profile-to-provider references, and
provider authentication fields. Load role prompts from the kit by validated
relative filename.

- [x] **Step 4: Run tests and verify GREEN**

Run: `PYTHONPATH=src python3 -m unittest tests.test_config -v`

Expected: all configuration tests pass.

### Task 2: Safe project compilation

**Files:**
- Create: `src/orchestra_kit/render.py`
- Create: `src/orchestra_kit/project.py`
- Test: `tests/test_project.py`

**Interfaces:**
- Consumes: `ProjectConfig`, project root, and template files.
- Produces: `init_project(project_root, kit_root, name)`, `sync_project(project_root, kit_root) -> SyncResult`, and `doctor_project(project_root, kit_root, environ) -> DoctorResult`.

- [x] **Step 1: Write failing project behavior tests**

```python
def test_sync_generates_model_bound_leaf_agent_without_secret(self):
    sync_project(self.project, self.kit)
    agent = (self.project / ".codex/agents/orchestra-worker.toml").read_text()
    self.assertIn('model = "deepseek-flash"', agent)
    self.assertIn('env_key = "DEEPSEEK_API_KEY"', agent)
    self.assertNotIn("secret-value", agent)

def test_sync_preserves_existing_agents_content(self):
    sync_project(self.project, self.kit)
    text = (self.project / "AGENTS.md").read_text()
    self.assertTrue(text.startswith("# Existing instructions"))

def test_sync_refuses_unmarked_agent_file(self):
    path = self.project / ".codex/agents/orchestra-worker.toml"
    path.parent.mkdir(parents=True)
    path.write_text("user owned")
    with self.assertRaisesRegex(ProjectError, "not managed"):
        sync_project(self.project, self.kit)
```

- [x] **Step 2: Run tests and verify RED**

Run: `PYTHONPATH=src python3 -m unittest tests.test_project -v`

Expected: FAIL because `orchestra_kit.project` does not exist.

- [x] **Step 3: Implement renderers, managed writes, manifest, init, sync, and doctor**

```python
GENERATED_MARKER = "Generated by OrchestraKit. Do not edit."
AGENTS_START = "<!-- orchestra-kit:start -->"
AGENTS_END = "<!-- orchestra-kit:end -->"
```

Render TOML using escaped JSON string literals, update only the marked
`AGENTS.md` block, atomically replace managed files, remove stale managed role
files listed by the previous manifest, and compare expected hashes in doctor.

- [x] **Step 4: Run tests and verify GREEN**

Run: `PYTHONPATH=src python3 -m unittest tests.test_project -v`

Expected: all project compilation tests pass.

### Task 3: Command-line workflow and portable documentation

**Files:**
- Create: `src/orchestra_kit/__main__.py`
- Create: `src/orchestra_kit/cli.py`
- Create: `bin/orchestra`
- Create: `tests/test_cli.py`
- Create: `README.md`
- Create: `docs/project-config.md`
- Create: `docs/providers/deepseek.md`
- Create: `.gitignore`
- Create: `pyproject.toml`

**Interfaces:**
- Consumes: the Task 2 project operations.
- Produces: `orchestra init`, `orchestra sync`, and `orchestra doctor` commands with stable exit codes and human-readable output.

- [x] **Step 1: Write failing CLI tests**

```python
def test_init_sync_and_doctor_work_end_to_end(self):
    initialized = self.run_cli("init", str(self.project), "--name", "Demo")
    self.assertEqual(initialized.returncode, 0, initialized.stderr)
    checked = self.run_cli("doctor", str(self.project))
    self.assertEqual(checked.returncode, 0, checked.stderr)

def test_doctor_fails_when_generated_file_drifted(self):
    self.run_cli("init", str(self.project), "--name", "Demo")
    agent = self.project / ".codex/agents/orchestra-worker.toml"
    agent.write_text(agent.read_text() + "\nchanged\n")
    checked = self.run_cli("doctor", str(self.project))
    self.assertEqual(checked.returncode, 1)
```

- [x] **Step 2: Run tests and verify RED**

Run: `PYTHONPATH=src python3 -m unittest tests.test_cli -v`

Expected: FAIL because the CLI module and launcher do not exist.

- [x] **Step 3: Implement CLI, launcher, packaging metadata, and user docs**

```python
parser = argparse.ArgumentParser(prog="orchestra")
subparsers = parser.add_subparsers(dest="command", required=True)
```

Document the three-command workflow, profile overrides, provider setup,
credential handling, safe regeneration, new-chat discovery, and a generic
example for connecting any local Codex project.

- [x] **Step 4: Run the complete test suite and smoke test**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests -v`

Run: `./bin/orchestra --help`

Run in a temporary directory: `./bin/orchestra init <temp-project> --name Demo`,
then `./bin/orchestra doctor <temp-project>`.

Expected: all tests pass, help exits `0`, init exits `0`, doctor reports a clean project.

### Task 4: Desktop repository and release check

**Files:**
- Create: local Git metadata for the completed OrchestraKit directory.
- Verify: every file in the repository and an independently initialized sample project.

**Interfaces:**
- Consumes: the verified source tree from Tasks 1–3.
- Produces: a local private Git repository on Desktop with one initial commit and no remote.

- [x] **Step 1: Verify requirement boundaries**

Search the repository case-insensitively for the retired backend name using a
shell-composed pattern that does not write the name into the repository.

Expected: no matches.

- [x] **Step 2: Copy the verified tree to Desktop and initialize local Git**

Run `git init -b main`, add only OrchestraKit files, and create the initial commit.
Do not add a remote.

- [x] **Step 3: Verify the Desktop artifact**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests -v`

Run: `git status --short --branch`

Run: `git remote -v`

Expected: tests pass, branch is clean, and no remote is configured.
