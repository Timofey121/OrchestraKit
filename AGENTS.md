# OrchestraKit contributor guidance

- Keep the kit independent of any particular user project, language, framework, or repository layout.
- Codex is always the root orchestrator. Generated agents are leaf executors and must never delegate.
- Preserve `.orchestra/project.toml` as the only project-specific source of truth.
- Do not write credentials or literal API keys to source, generated files, tests, or documentation.
- Add or change behavior with a failing `unittest` first.
- Run `PYTHONPATH=src python3 -m unittest discover -s tests -v` before claiming completion.

<!-- orchestra-kit:start -->
## OrchestraKit

For substantial work with independent tracks, use the `$orchestrate-project`
skill and the generated custom agents: `orchestra_explorer`, `orchestra_worker`, `orchestra_tester`, `orchestra_reviewer`. Codex remains the sole root
orchestrator and must verify leaf-agent results before integration. Keep small or
tightly coupled work in the root session. Project orchestration settings live in
`.orchestra/project.toml`; the generated execution contract defines workflow
gates and authority boundaries. After editing settings, run `orchestra sync`
from the kit.
<!-- orchestra-kit:end -->
