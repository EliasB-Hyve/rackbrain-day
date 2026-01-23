# rackbrain/cli/main.py

import argparse
from typing import Any, Dict, List

from rackbrain.adapters.jira_client import JiraClient
from rackbrain.core.config_loader import load_app_config
from rackbrain.core.rules_engine import load_rules_from_files
from rackbrain.services.ticket_processor import process_ticket
from rackbrain.services.polling_service import (
    run_polling_loop,
    build_default_jql,
)
from rackbrain.services.logger import init_logger
try:
    from rackbrain.services.metrics import (
        generate_daily_summary,
        print_summary,
    )
except ImportError:
    # Metrics module may not be available in all environments
    generate_daily_summary = None
    print_summary = None


def main() -> None:
    parser = argparse.ArgumentParser(prog="rackbrain")
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config YAML (overrides $RACKBRAIN_CONFIG and defaults)",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.required = True  # Python 3.6 style

    # rackbrain process-ticket MFGS-440739 --dry-run
    parser_process = subparsers.add_parser("process-ticket")
    parser_process.add_argument("issue_key")
    parser_process.add_argument("--apply", action="store_false", dest="dry_run",
                                help="Apply changes (post comment) instead of dry run")
    parser_process.add_argument("--skip-commands", action="store_true",
                                help="Skip EVE command execution for faster dry-runs (only test templates)")

    # rackbrain poll --apply
    parser_poll = subparsers.add_parser("poll")
    parser_poll.add_argument("--apply", action="store_false", dest="dry_run",
                            help="Apply changes (post comment) instead of dry run")
    parser_poll.add_argument("--skip-commands", action="store_true",
                            help="Skip EVE command execution for faster processing")
    parser_poll.add_argument("--once", action="store_true",
                            help="Run once and exit (don't poll continuously)")
    parser_poll.add_argument("--jql", type=str, default=None,
                            help="Custom JQL query (overrides config)")
    parser_poll.add_argument("--interval", type=int, default=None,
                            help="Poll interval in seconds (overrides config)")

    # rackbrain metrics --date 2025-01-15
    parser_metrics = subparsers.add_parser("metrics")
    parser_metrics.add_argument("--date", type=str, default=None,
                               help="Date to analyze (YYYY-MM-DD, default: today)")
    parser_metrics.add_argument("--days", type=int, default=1,
                               help="Number of days to analyze (default: 1)")
    parser_metrics.add_argument("--format", choices=["text", "json"], default="text",
                               help="Output format (default: text)")

    # rackbrain doctor
    parser_doctor = subparsers.add_parser("doctor")
    parser_doctor.add_argument("--check-db", action="store_true", help="Also check DB env vars are set")

    args = parser.parse_args()

    # ---- Load config ----
    config, config_path, base_dir = load_app_config(getattr(args, "config", None))

    jira_cfg = config.get("jira", {})
    base_url = jira_cfg.get("base_url", "")
    pat = jira_cfg.get("pat", "")
    pat_env = jira_cfg.get("pat_env", "RACKBRAIN_JIRA_PAT")

    # ---- Initialize logging ----
    logger = init_logger(config)
    if logger:
        print("[INFO] Logging enabled: %s" % logger._get_log_path())

    print("[INFO] Using config: %s" % config_path)
    print("[INFO] Base dir: %s" % base_dir)

    # Get processing config
    processing_config = config.get("processing", {})

    if args.command == "process-ticket":
        if not base_url:
            raise RuntimeError("jira.base_url is not set in config/config.yaml")

        rules_cfg = config.get("rules", {})
        rule_files = rules_cfg.get("files", [])
        if not rule_files:
            raise RuntimeError("No rules files configured under rules.files in config/config.yaml")
        rules = load_rules_from_files(rule_files)
        print("[INFO] Loaded %d rule(s) from %s" % (len(rules), rule_files))

        jira = JiraClient(base_url=base_url, pat=pat, pat_env=pat_env)
        dry_run = args.dry_run
        skip_commands = getattr(args, "skip_commands", False)

        process_ticket(
            jira=jira,
            rules=rules,
            issue_key=args.issue_key,
            dry_run=dry_run,
            skip_commands=skip_commands,
            processing_config=processing_config,
        )

    elif args.command == "poll":
        if not base_url:
            raise RuntimeError("jira.base_url is not set in config/config.yaml")

        rules_cfg = config.get("rules", {})
        rule_files = rules_cfg.get("files", [])
        if not rule_files:
            raise RuntimeError("No rules files configured under rules.files in config/config.yaml")
        rules = load_rules_from_files(rule_files)
        print("[INFO] Loaded %d rule(s) from %s" % (len(rules), rule_files))

        jira = JiraClient(base_url=base_url, pat=pat, pat_env=pat_env)
        polling_cfg = config.get("polling", {})
        
        # Build JQL query
        if args.jql:
            jql = args.jql
            print(f"[INFO] Using custom JQL from command line")
        elif polling_cfg.get("jql"):
            jql = polling_cfg["jql"]
            print(f"[INFO] Using custom JQL from config")
        else:
            # Build default JQL from config
            project_key = polling_cfg.get("project_key", "MFGS")
            allowed_statuses = polling_cfg.get("allowed_statuses", ["Open", "In Progress"])
            lookback_hours = polling_cfg.get("lookback_hours", 1)
            jql = build_default_jql(
                project_key=project_key,
                allowed_statuses=allowed_statuses,
                lookback_hours=lookback_hours,
            )
            print(f"[INFO] Using default JQL (project={project_key}, statuses={allowed_statuses}, lookback={lookback_hours}h)")

        # Get polling settings
        poll_interval = args.interval or polling_cfg.get("poll_interval_seconds", 120)
        max_workers = polling_cfg.get("max_workers", 4)
        max_results = polling_cfg.get("max_results", 200)
        dry_run = args.dry_run
        skip_commands = getattr(args, "skip_commands", False)
        run_once = getattr(args, "once", False)

        run_polling_loop(
            jira=jira,
            rules=rules,
            jql=jql,
            poll_interval_seconds=poll_interval,
            dry_run=dry_run,
            skip_commands=skip_commands,
            max_workers=max_workers,
            max_results=max_results,
            run_once=run_once,
            app_config=config,
            processing_config=processing_config,
        )

    elif args.command == "metrics":
        if generate_daily_summary is None or print_summary is None:
            print("ERROR: Metrics module not available. Check installation.")
            return
        
        logging_cfg = config.get("logging", {})
        log_dir = logging_cfg.get("log_dir", "logs")
        
        if args.format == "json":
            import json
            summary = generate_daily_summary(log_dir=log_dir, date=args.date)
            print(json.dumps(summary, indent=2))
        else:
            summary = generate_daily_summary(log_dir=log_dir, date=args.date)
            print_summary(summary)

    elif args.command == "doctor":
        import os
        from pathlib import Path

        print("[OK] Config file: %s" % config_path)
        print("[OK] Base dir: %s" % base_dir)

        rules_cfg = config.get("rules", {})
        rule_files = rules_cfg.get("files", [])
        if not rule_files:
            print("[WARN] rules.files is empty (no rules will load)")
        else:
            missing = [p for p in rule_files if not Path(p).exists()]
            if missing:
                print("[FAIL] Missing rules file(s): %s" % ", ".join(missing))
            else:
                print("[OK] rules.files: %d file(s) found" % len(rule_files))

        pat_value = (pat or "").strip() or os.environ.get(pat_env, "").strip()
        if pat_value:
            print("[OK] Jira PAT available via %s" % (("config" if (pat or "").strip() else pat_env),))
        else:
            print("[FAIL] Jira PAT missing: set %s (recommended) or jira.pat in config" % pat_env)

        if getattr(args, "check_db", False):
            missing = [k for k in ["RACKBRAIN_DB_HOST", "RACKBRAIN_DB_USER", "RACKBRAIN_DB_PASS", "RACKBRAIN_DB_NAME"] if not os.environ.get(k, "").strip()]
            if missing:
                print("[WARN] DB env vars missing (DB lookups will be skipped): %s" % ", ".join(missing))
            else:
                print("[OK] DB env vars present")


if __name__ == "__main__":
    main()
