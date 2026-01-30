# Agent guide

This repo is safe to edit with an agent as long as you keep the workflow conservative and avoid
secrets.

## Read first

- `docs/README.md` (repo map)
- `docs/ARCHITECTURE.md` (runtime flow + side effects)
- `docs/RULES.md` (canonical rules reference)
- `docs/INTEGRATIONS.md` (what depends on env vars and external systems)
- `docs/RUNBOOK.md` (operator workflow and safety defaults)

## Safety rules

- Do not add secrets to the repo (PATs, DB passwords, cookies).
- Dry-run is the default; do not use `--apply` unless explicitly asked.
- When testing rule edits, start with `--skip-commands`.
- Keep changes focused: rule changes should not include unrelated refactors.

## Where to make changes

- Rules: `config/rules/*.yaml`
- Config reference/example: `config/CONFIG_REFERENCE.md`, `config/config.example.yaml`
- Runtime code: `rackbrain/`

## Quick validation checklist

- Config/rule load: `python -m rackbrain --config config/config.example.yaml doctor`
- Single-ticket dry-run:
  `python -m rackbrain --config config/config.yaml process-ticket MFGS-123456 --skip-commands`

## Repo hygiene

- Line endings are normalized to LF via `.gitattributes`.
- Prefer wrapping Markdown around 100 characters for readability.
