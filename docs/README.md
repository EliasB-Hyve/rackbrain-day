# RackBrain documentation

This folder is the “human and agent” index for RackBrain.

If you are new to the repo:

- Start here: `README.md` (top-level overview and quick start)
- Then read: `docs/ARCHITECTURE.md` (what calls what, and where side effects happen)
- If you operate the bot: `docs/RUNBOOK.md`
- If you write rules/templates: `docs/RULE_AUTHORING.md`, `config/RULES_REFERENCE.md`,
  `config/rules/RULES_YAML_REFERENCE.md`
- For config keys and path semantics: `config/CONFIG_REFERENCE.md`

## Repository map

**Runtime code (`rackbrain/`)**

- CLI entry: `rackbrain/cli/main.py`
- Config discovery + normalization: `rackbrain/core/config_loader.py`
- Rule parsing: `rackbrain/core/rules_engine.py`
- Rule matching + selection: `rackbrain/core/classification.py`
- Jira → Ticket/ErrorEvent extraction + enrichment: `rackbrain/core/context_builder.py`
- Ticket processing orchestrator: `rackbrain/services/ticket_processor.py`
- Comment rendering (templates + placeholders): `rackbrain/services/comment_renderer.py`
- Jira side effects (assign/transition/comment/link): `rackbrain/services/jira_actions.py`
- Polling loop + extra queries: `rackbrain/services/polling_service.py`
- Command step execution: `rackbrain/services/command_steps.py`
- TestView integration helpers: `rackbrain/services/testview_actions.py`
- Timer suppression store (SQLite): `rackbrain/services/timer_store.py`
- Processing logs + rule match history: `rackbrain/services/logger.py`
- Metrics summarizer for JSON logs: `rackbrain/services/metrics.py`

**Rules and config (`config/`)**

- Example config: `config/config.example.yaml` (copy to `config/config.yaml` locally)
- Config reference: `config/CONFIG_REFERENCE.md`
- Rule schema reference: `config/RULES_REFERENCE.md`
- Rules live in: `config/rules/*.yaml`

**External helpers (repo root)**

- DB env-var bridge: `database_config.py` (used by `rackbrain/adapters/hyvetest_client.py`)
- TestView helper module: `Testviewlog.py` (imported by `rackbrain/core/testview_context.py`
  and `rackbrain/services/testview_actions.py`)
- EVE command runner (bash, Linux): `eve_cmd_runner.sh`

**Scripts**

- Bootstrap venv and dependencies: `scripts/bootstrap.sh`
- Wrapper to run the CLI from anywhere: `scripts/rackbrain`
- Health check: `scripts/health_check.sh`

## How RackBrain runs (mental model)

RackBrain is “config + rules + integrations”:

- Config tells RackBrain where to find rule files, where to write logs/state, and what default Jira
  actions should be applied.
- Rules decide *when* a ticket matches and *what* action/template to use.
- Integrations enrich the ticket context so rules/templates can be more precise:
  - Jira (required): reads ticket fields; optionally writes transitions/comments.
  - hyvetest DB (optional): fetches failure_message/testset/etc for better matching.
  - TestView (optional): fetches log snippets; can start SLT/PRETEST.
  - EVE/ILOM commands (optional): collects ILOM/diag output for comments/workflows.
  - Cinder verification (special-case): builds a report for specific “Outpost refurb” tickets.

## Safety defaults

- CLI defaults to dry-run: it prints the comment it *would* post. Use `--apply` to make Jira edits.
- Use `--skip-commands` until you are confident your EVE runner path works in your environment.
- Keep secrets out of the repo: Jira PAT, DB passwords, TestView cookies are environment variables.
