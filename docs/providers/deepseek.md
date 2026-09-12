# DeepSeek provider

DeepSeek documents a native Codex integration through the Responses API. Its
current instructions and model catalog are published at:

<https://api-docs.deepseek.com/quick_start/agent_integrations/codex/>

## Project-scoped setup

1. Copy the current `models.json` content from the official integration page to
   `.orchestra/providers/deepseek-models.json` in the target project.
2. Export the key in the environment that launches Codex:

   ```bash
   export DEEPSEEK_API_KEY="your-key"
   ```

3. Add the provider and select it from a profile:

   ```toml
   [providers.deepseek]
   name = "DeepSeek"
   base_url = "https://api.deepseek.com/"
   env_key = "DEEPSEEK_API_KEY"
   wire_api = "responses"
   model_catalog_json = ".orchestra/providers/deepseek-models.json"
   supports_standalone_web_search = true
   capabilities = ["function", "apply_patch", "web_search"]

   [profiles.cheap]
   model = "deepseek-flash"
   effort = "high"
   provider = "deepseek"
   ```

4. Run `orchestra sync` and `orchestra doctor`.
5. Start a new Codex chat for the project so the regenerated custom agents are
   loaded.

Do not put the literal key in TOML. The generated custom agent references only
`DEEPSEEK_API_KEY`.

DeepSeek's Responses API supports a narrower tool surface than native Codex
models. Keep provider-dependent roles bounded, and use a native profile for work
that requires unsupported tools. Check the current limitations before changing
role mappings:

<https://api-docs.deepseek.com/guides/responses_api/>
