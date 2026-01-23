# RackBrain LLM Dataset Contract

This document locks the JSON schema produced by `build_llm_dataset.py`. This contract is
explicitly **offline** and **append-only**: published records must never be mutated or
removed, and new records may only be appended. Consumers should assume the dataset is not
served by a live API and is updated only by publishing new files.

## Stability guarantees (must never change once published)

* Top-level keys and their meanings are fixed: `id`, `created`, `updated`, `sn`,
  `source_links`, `text`, `signals`, `labels`.
* Field types and cardinality are fixed (see schema below).
* Nested keys inside `text`, `signals`, `labels`, and `text.testview_compact` are fixed.
* Existing fields must not be renamed, removed, or repurposed.
* New fields must not be added without a versioned contract update (no silent extensions).

## JSON schema (summary)

Each record is a single JSON object with the following required keys:

| Field | Required | Type | Cardinality |
| --- | --- | --- | --- |
| `id` | Yes | `string` or `null` | Single |
| `created` | Yes | `string` or `null` | Single |
| `updated` | Yes | `string` or `null` | Single |
| `sn` | Yes | `string` or `null` | Single |
| `source_links` | Yes | `array<string>` | List |
| `text` | Yes | `object` | Single |
| `signals` | Yes | `object` | Single |
| `labels` | Yes | `object` | Single |

### `text` object

Required keys:

| Field | Required | Type | Cardinality |
| --- | --- | --- | --- |
| `summary` | Yes | `string` | Single |
| `description` | Yes | `string` | Single |
| `comments_compact` | Yes | `string` | Single |
| `testview_compact` | Yes | `object` | Single |

`testview_compact` is an object that may be empty. When present, it may include only the
following optional keys:

| Field | Required | Type | Cardinality |
| --- | --- | --- | --- |
| `download_ok` | No | `boolean` or `null` | Single |
| `failed_testset` | No | `string` or `null` | Single |
| `failed_testcase` | No | `string` or `null` | Single |
| `log_excerpt` | No | `string` | Single |

### `signals` object

Required keys (all lists of strings):

| Field | Required | Type | Cardinality |
| --- | --- | --- | --- |
| `keywords` | Yes | `array<string>` | List |
| `components` | Yes | `array<string>` | List |
| `error_signatures` | Yes | `array<string>` | List |
| `ports` | Yes | `array<string>` | List |
| `lanes` | Yes | `array<string>` | List |

### `labels` object

Required keys:

| Field | Required | Type | Cardinality |
| --- | --- | --- | --- |
| `rackbrain_match` | Yes | `boolean` or `null` | Single |
| `matched_rule_id` | Yes | `string` or `null` | Single |
| `observed_action` | Yes | `string` or `null` | Single |
| `resolution` | Yes | `string` or `null` | Single |
