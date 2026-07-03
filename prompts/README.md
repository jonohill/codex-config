# Prompt files

These two Markdown files are injected as the configured model's
`base_instructions` (concatenated as base + supplement):

- `codex-base-instructions.md` — the verbatim Codex `BASE_INSTRUCTIONS`
  (from `codex-rs/models-manager/prompt.md`). Edit only if you want to diverge
  from the shipped harness prompt.
- `codex-supplement-instructions.md` — supplemental sandbox/escalation
  guidance appended to the base prompt. Edit freely.

`configure-codex.py` reads these files at run time, so changes here take effect
on the next run without touching the script.
