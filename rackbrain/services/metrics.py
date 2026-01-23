# rackbrain/services/metrics.py

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional
from collections import defaultdict


def load_log_entries(log_dir: str = "logs", days: int = 1) -> List[Dict[str, Any]]:
    """
    Load log entries from log files.

    Args:
        log_dir: Directory containing log files
        days: How many days back to load (default: 1)

    Returns:
        List of log entry dictionaries
    """
    log_path = Path(log_dir)
    if not log_path.exists():
        return []

    entries = []
    cutoff_date = datetime.now() - timedelta(days=days)

    # Load all log files from the last N days
    for log_file in log_path.glob("rackbrain_processed_*.log"):
        try:
            # Extract date from filename
            date_str = log_file.stem.replace("rackbrain_processed_", "")
            file_date = datetime.strptime(date_str, "%Y-%m-%d")
            
            if file_date >= cutoff_date:
                with log_file.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            entries.append(entry)
                        except json.JSONDecodeError:
                            # Skip invalid JSON lines
                            continue
        except (ValueError, Exception):
            # Skip files with invalid names or unreadable
            continue

    return entries


def calculate_automation_rate(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Calculate automation rate and related metrics.

    Returns:
        Dict with automation_rate, total_processed, successful, etc.
    """
    if not entries:
        return {
            "automation_rate": 0.0,
            "total_processed": 0,
            "successful": 0,
            "failed": 0,
            "no_match": 0,
            "dry_run": 0,
        }

    total = len(entries)
    successful = sum(1 for e in entries if e.get("success") and not e.get("dry_run"))
    failed = sum(1 for e in entries if not e.get("success"))
    no_match = sum(1 for e in entries if e.get("actions_taken", {}).get("action") == "no_match")
    dry_run = sum(1 for e in entries if e.get("dry_run"))

    automation_rate = (successful / total * 100) if total > 0 else 0.0

    return {
        "automation_rate": round(automation_rate, 2),
        "total_processed": total,
        "successful": successful,
        "failed": failed,
        "no_match": no_match,
        "dry_run": dry_run,
    }


def calculate_rule_statistics(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Calculate statistics per rule.

    Returns:
        List of dicts with rule_id, rule_name, count, avg_confidence
    """
    rule_stats = defaultdict(lambda: {"count": 0, "confidences": [], "rule_name": None})

    for entry in entries:
        rule_id = entry.get("rule_id")
        if not rule_id:
            continue

        rule_stats[rule_id]["count"] += 1
        rule_stats[rule_id]["rule_name"] = entry.get("rule_name")
        if entry.get("confidence") is not None:
            rule_stats[rule_id]["confidences"].append(entry.get("confidence"))

    results = []
    for rule_id, stats in rule_stats.items():
        avg_confidence = (
            sum(stats["confidences"]) / len(stats["confidences"])
            if stats["confidences"]
            else 0.0
        )
        results.append({
            "rule_id": rule_id,
            "rule_name": stats["rule_name"],
            "match_count": stats["count"],
            "avg_confidence": round(avg_confidence, 2),
        })

    return sorted(results, key=lambda x: x["match_count"], reverse=True)


def generate_daily_summary(log_dir: str = "logs", date: Optional[str] = None) -> Dict[str, Any]:
    """
    Generate a daily summary report.

    Args:
        log_dir: Directory containing logs
        date: Date string (YYYY-MM-DD) or None for today

    Returns:
        Summary dictionary
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    # Load entries for the specified date
    entries = load_log_entries(log_dir, days=30)  # Load last 30 days
    date_entries = [
        e for e in entries
        if e.get("timestamp", "").startswith(date)
    ]

    automation = calculate_automation_rate(date_entries)
    rule_stats = calculate_rule_statistics(date_entries)

    return {
        "date": date,
        "automation": automation,
        "top_rules": rule_stats[:10],  # Top 10 rules
        "total_rules_matched": len(rule_stats),
    }


def print_summary(summary: Dict[str, Any]) -> None:
    """Print a human-readable summary."""
    print(f"\n=== RackBrain Daily Summary - {summary['date']} ===\n")
    
    auto = summary["automation"]
    print(f"Total Processed: {auto['total_processed']}")
    print(f"Successfully Automated: {auto['successful']}")
    print(f"No Rule Match: {auto['no_match']}")
    print(f"Failed: {auto['failed']}")
    print(f"Dry Runs: {auto['dry_run']}")
    
    if auto["total_processed"] > 0:
        print(f"\nAutomation Rate: {auto['automation_rate']}%")
    
    if summary["top_rules"]:
        print(f"\nTop Rules (by match count):")
        for rule in summary["top_rules"]:
            print(f"  {rule['rule_id']}: {rule['match_count']} matches (avg confidence: {rule['avg_confidence']:.2f})")
    
    print()


