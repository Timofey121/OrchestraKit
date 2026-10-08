# Local observation dashboard

Start `ui PROJECT` through the skill launcher to open a loopback-only dashboard.
The command can receive several project paths. It reads known local Codex
projects and chat metadata unless `--no-codex` is supplied. Use `--no-browser`
to leave navigation to the user and `--port NUMBER` to change the default 8731.
The server stays in the foreground until stopped.

Monitoring reads local state without dispatching or cancelling execution or
calling models. Explicit settings saves update project profiles and generated
instructions; explicit connection creates the project integration. Writes require
a known project, same-origin request and a session token. API keys stay in the
macOS Keychain or environment. Unknown usage remains unknown. Historical
task status is separate from current verification freshness and chat activity.
Execution models are requested launch settings, not provider attestations.

Tasks started by the CLI retain `CODEX_THREAD_ID` when present. Outside Codex,
`task start PROJECT --goal TEXT --chat-id UUID` sets an explicit association.
Never infer a task/chat association from recency or similar wording.
