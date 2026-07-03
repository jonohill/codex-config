# codex-config

Script to configure the Codex CLI to use a cloud model through its
OpenAI-compatible endpoint. Ships with defaults for Ollama Cloud / GLM 5.2.

- `config.default.toml` — provider/model defaults (Ollama Cloud / GLM 5.2).
- `prompts/` — base + supplemental system prompts injected as the model's
  `base_instructions`; edit them without touching the script.

Provider and model settings live in `config.default.toml` — edit it in place
or pass `--config <path>` to target a different provider or model.

## Usage

```sh
# defaults (Ollama Cloud / GLM 5.2)
./configure-codex.py

# custom provider/model config
./configure-codex.py --config ./my-provider.toml

# ad-hoc overrides (highest priority first: flag > env > config)
./configure-codex.py --model glm-5.2:cloud --base-url https://ollama.com/v1
```

Env overrides: `CODEX_MODEL`, `CODEX_BASE_URL`, `CODEX_CONTEXT_WINDOW`,
`<provider.env_key>`, `CODEX_HOME`.
