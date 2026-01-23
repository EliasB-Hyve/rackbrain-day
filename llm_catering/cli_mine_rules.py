"""CLI tool to cluster LLM dataset records into candidate RackBrain rule families."""

from __future__ import absolute_import

import argparse
import hashlib
import json
import sys

from .rule_mining import (
    cluster_records,
    excerpt_text,
    gather_text_fields,
    load_llm_dataset,
    serialize_cluster_key,
    top_terms,
)


def mine_rule_clusters(input_path, output_path):
    records = load_llm_dataset(input_path)
    clusters = cluster_records(records)

    cluster_summaries = []
    for key, cluster_records_list in clusters.items():
        cluster_id = _cluster_id(key)
        issue_keys = [record.get("id") for record in cluster_records_list]
        issue_keys = [key for key in issue_keys if key]
        issue_keys = sorted(set(issue_keys))
        texts = [gather_text_fields(record) for record in cluster_records_list]
        excerpt = ""
        for text in texts:
            if text:
                excerpt = excerpt_text(text)
                break
        cluster_summaries.append(
            {
                "cluster_id": cluster_id,
                "count": len(cluster_records_list),
                "cluster_key": serialize_cluster_key(key),
                "top_terms": top_terms(texts),
                "example_ids": issue_keys[:5],
                "example_excerpt": excerpt,
            }
        )

    cluster_summaries.sort(
        key=lambda item: (-item["count"], item["cluster_id"])
    )

    with open(output_path, "w") as handle:
        for summary in cluster_summaries:
            handle.write(json.dumps(summary, sort_keys=True) + "\n")

    _print_summary(len(records), cluster_summaries)


def _cluster_id(key):
    payload = json.dumps(serialize_cluster_key(key), sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _print_summary(total_records, cluster_summaries):
    print("total records: {0}".format(total_records))
    print("total clusters: {0}".format(len(cluster_summaries)))
    print("top 10 clusters by size:")
    for summary in cluster_summaries[:10]:
        print(
            "- {cluster_id} count={count} key={key}".format(
                cluster_id=summary["cluster_id"],
                count=summary["count"],
                key=summary["cluster_key"],
            )
        )


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Cluster LLM dataset records into candidate rule families."
    )
    parser.add_argument("--in", dest="input_path", required=True, help="Input JSONL")
    parser.add_argument("--out", dest="output_path", required=True, help="Output JSONL")
    return parser.parse_args(argv)


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    args = _parse_args(argv)
    mine_rule_clusters(args.input_path, args.output_path)


if __name__ == "__main__":
    main()
