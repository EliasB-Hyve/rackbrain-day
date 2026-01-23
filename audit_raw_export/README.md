# Audit Raw Export

`audit_raw_export.py` exports Jira issues to JSONL, capturing the issue summary, description,
comments, and derived combined text fields. It extracts SN values from the text and looks for
TestView `slt/testdetail` URLs. When a TestView URL is found, it attempts to download
`log.raw` via the TestView detail page and includes the download results alongside the
exported issue record.

## Usage

Smoke test (exports a single known issue):

```bash
python3 audit_raw_export/audit_raw_export.py --smoke-test
```

Config-driven export (uses `audit_raw_export_config.yaml`):

```bash
python3 audit_raw_export/audit_raw_export.py
```

Override JQL and output prefix:

```bash
python3 audit_raw_export/audit_raw_export.py --jql "project = MFGS" --out output/my_export
```

## TestView requirements

Set the `HYVE_TESTVIEW_COOKIE` environment variable to allow TestView `log.raw` downloads.
Without it, the export still runs but skips TestView retrieval.

## Output JSONL schema (high level)

Each JSONL line contains a record with fields such as:

- `issue_key`, `created`, `updated`
- `summary`, `description`, `comments`
- `combined_text`, `combined_text_with_comments`,
  `combined_text_with_comments_and_logs`
- `sn`
- `links` (Jira URL, test detail URL, jar URL when detected)
- `testview` (download status, artifacts, log text when available)
