# LLM Catering

This package provides lightweight helpers for building LLM-ready datasets from RackBrain audit
export JSONL files.

## Usage

```bash
python3 -m llm_catering.build_llm_dataset \
  --in audit_export.jsonl \
  --out outputs/llm_dataset.jsonl
```

The script reads JSONL records, compacts comments and testview details, extracts signals, and
writes a normalized JSONL dataset suitable for LLM training or evaluation.
