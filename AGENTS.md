# AGENTS.md

## Codex catalog/config changes

Before changing anything that writes Codex model-catalog JSON or `config.toml`
fields, **always check the Codex source** to confirm the real schema and valid
option values. Do not guess field names or enum values.

- Source: https://github.com/openai/codex (clone to `/tmp` if needed)
  - Catalog schema / model metadata: `codex-rs/protocol/src/openai_models.rs`
    (`ModelInfo`, `ReasoningEffortPreset`, `ReasoningEffort`, `ModelPreset`)
  - Bundled reference catalog: `codex-rs/models-manager/models.json`
- The installed binary is the source of truth for what the running version
  accepts. Validate any generated catalog with:

  ```sh
  CODEX_HOME=<test-home> codex debug models
  ```

  It parses the catalog through the real `ModelInfo` struct, so malformed
  shapes (e.g. bare strings where preset objects are expected) fail loudly.

## Validate with codex doctor

When making config/catalog changes, run the diagnostics before finishing:

```sh
CODEX_HOME=<test-home> codex doctor
```

Address any `✗` / `⚠` entries it reports that relate to your change. Note:
`reachability` may show `✗` in offline/sandboxed environments — that is an
expected network restriction, not a config bug.
