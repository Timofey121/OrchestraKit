# OrchestraKit contributor guidance

- Keep the kit independent of any particular user project, language, framework, or repository layout.
- Codex is always the root orchestrator. Generated agents are leaf executors and must never delegate.
- Preserve `.orchestra/project.toml` as the only project-specific source of truth.
- Do not write credentials or literal API keys to source, generated files, tests, or documentation.
- Add or change behavior with a failing `unittest` first.
- Run `PYTHONPATH=src python3 -m unittest discover -s tests -v` before claiming completion.

