#!/usr/bin/env python3
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#

import argparse
import csv
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pulsar_audit_common import (
    AuditError,
    HttpError,
    Kubectl,
    PortForward,
    http_json,
    schema_parts,
    write_json,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate non-destructive guardrails for RF=1 schema candidates."
    )
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--namespace", default="pulsar")
    parser.add_argument("--release", default="pulsar")
    parser.add_argument("--context")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--schema", action="append", dest="schemas")
    parser.add_argument("--http-timeout", default=15, type=int)
    parser.add_argument("--max-inventory-age-seconds", default=900, type=int)
    return parser.parse_args()


def topic_url(
    proxy_url: str,
    schema_id: str,
    suffix: str,
    domain: str = "persistent",
) -> str:
    tenant, namespace, topic = schema_parts(schema_id)
    return (
        f"{proxy_url}/admin/v2/{domain}/{quote(tenant, safe='')}/"
        f"{quote(namespace, safe='')}/{quote(topic, safe='')}/{suffix}"
    )


def sum_field(payloads: list[dict[str, Any]], field: str) -> int:
    return sum(int(payload.get(field, 0) or 0) for payload in payloads)


def normalize_stats(stats: dict[str, Any]) -> dict[str, Any]:
    partitions = list(stats.get("partitions", {}).values())
    payloads = partitions or [stats]
    publishers = []
    subscriptions: dict[str, dict[str, int]] = {}
    for payload in payloads:
        publishers.extend(payload.get("publishers", []))
        for name, subscription in payload.get("subscriptions", {}).items():
            totals = subscriptions.setdefault(
                name, {"msgBacklog": 0, "backlogSize": 0, "unackedMessages": 0}
            )
            for field in totals:
                totals[field] += int(subscription.get(field, 0) or 0)
    return {
        "storageSize": sum_field(payloads, "storageSize"),
        "backlogSize": sum_field(payloads, "backlogSize"),
        "offloadedStorageSize": sum_field(payloads, "offloadedStorageSize"),
        "replicationBacklog": sum(
            int(replication.get("replicationBacklog", 0) or 0)
            for payload in payloads
            for replication in payload.get("replication", {}).values()
        ),
        "publishers": publishers,
        "subscriptions": subscriptions,
        "partitionCount": len(partitions),
    }


def normalize_internal_stats(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    compacted_ledgers = [payload.get("compactedLedger", {}) for payload in payloads]
    ledgers = [ledger for payload in payloads for ledger in payload.get("ledgers", [])]
    return {
        "numberOfEntries": sum_field(payloads, "numberOfEntries"),
        "totalSize": sum_field(payloads, "totalSize"),
        "pendingAddEntriesCount": sum_field(payloads, "pendingAddEntriesCount"),
        "compactedLedgerEntries": sum(
            max(int(ledger.get("entries", 0) or 0), 0) for ledger in compacted_ledgers
        ),
        "compactedLedgerSize": sum(
            max(int(ledger.get("size", 0) or 0), 0) for ledger in compacted_ledgers
        ),
        "offloadedManagedLedgers": sum(
            bool(ledger.get("offloaded")) for ledger in ledgers
        ),
    }


def publisher_summary(publisher: dict[str, Any]) -> dict[str, Any]:
    return {
        key: publisher.get(key)
        for key in (
            "producerName",
            "address",
            "connectedSince",
            "clientVersion",
            "metadata",
        )
        if publisher.get(key) is not None
    }


def validate_schema(
    proxy_url: str,
    schema_id: str,
    ledgers: list[dict[str, Any]],
    current_ledger_id: int | None,
    current_ledger_rf: int | None,
    timeout: int,
) -> dict[str, Any]:
    referenced_rf1 = [
        ledger
        for ledger in ledgers
        if ledger["ensembleSize"] == 1 and ledger["referenced"] is True
    ]
    orphan_rf1 = [
        ledger
        for ledger in ledgers
        if ledger["ensembleSize"] == 1 and ledger["referenced"] is False
    ]
    unknown_rf1 = [
        ledger
        for ledger in ledgers
        if ledger["ensembleSize"] == 1 and ledger["referenced"] is None
    ]
    result: dict[str, Any] = {
        "schemaId": schema_id,
        "referencedRf1LedgerIds": [ledger["ledgerId"] for ledger in referenced_rf1],
        "unreferencedRf1LedgerIds": [ledger["ledgerId"] for ledger in orphan_rf1],
        "unknownReferenceRf1LedgerIds": [
            ledger["ledgerId"] for ledger in unknown_rf1
        ],
        "currentLedgerId": current_ledger_id,
        "currentLedgerRf": current_ledger_rf,
        "currentLedgerIsRf1": current_ledger_rf == 1,
        "status": None,
        "dataGuardrailsPassed": False,
        "producerStopRequired": bool(referenced_rf1),
    }
    if unknown_rf1:
        result["status"] = "blocked_unknown_reference"
        return result
    if not referenced_rf1:
        result["status"] = "orphan_only_not_fix2"
        return result

    try:
        partition_metadata = http_json(
            topic_url(proxy_url, schema_id, "partitions"), timeout=timeout
        )
        partition_count = int(partition_metadata.get("partitions", 0) or 0)
        if partition_count:
            stats_suffix = (
                "partitioned-stats?perPartition=true&getPreciseBacklog=true"
                "&subscriptionBacklogSize=true"
            )
        else:
            stats_suffix = (
                "stats?getPreciseBacklog=true&subscriptionBacklogSize=true"
            )
        stats = http_json(
            topic_url(proxy_url, schema_id, stats_suffix), timeout=timeout
        )
        normalized = normalize_stats(stats)
        try:
            non_persistent_stats = http_json(
                topic_url(
                    proxy_url,
                    schema_id,
                    "stats?getPreciseBacklog=true&subscriptionBacklogSize=true",
                    domain="non-persistent",
                ),
                timeout=timeout,
            )
        except HttpError as error:
            if error.status != 404:
                raise
        else:
            result.update(
                {
                    "status": "blocked_non_persistent_topic_exists",
                    "nonPersistentStats": normalize_stats(non_persistent_stats),
                }
            )
            return result
        if partition_count:
            observed_partitions = len(stats.get("partitions", {}))
            if observed_partitions != partition_count:
                result.update(
                    {
                        "status": "blocked_incomplete_partition_stats",
                        "expectedPartitions": partition_count,
                        "observedPartitions": observed_partitions,
                    }
                )
                return result
            normalized = normalize_stats(stats)
            tenant, namespace, topic = schema_parts(schema_id)
            internal_payloads = [
                http_json(
                    topic_url(
                        proxy_url,
                        f"{tenant}/{namespace}/{topic}-partition-{partition}",
                        "internalStats?includeLedgerMetadata=true",
                    ),
                    timeout=timeout,
                )
                for partition in range(partition_count)
            ]
        else:
            internal_payloads = [
                http_json(
                    topic_url(
                        proxy_url,
                        schema_id,
                        "internalStats?includeLedgerMetadata=true",
                    ),
                    timeout=timeout,
                )
            ]
        internal = normalize_internal_stats(internal_payloads)
    except HttpError as error:
        result.update(
            {
                "status": "blocked_stats_error",
                "httpStatus": error.status,
                "error": error.body[:2000],
            }
        )
        return result

    subscriptions = normalized["subscriptions"]
    backlog = normalized["backlogSize"]
    subscription_backlog = sum(
        subscription["msgBacklog"] for subscription in subscriptions.values()
    )
    unacked = sum(
        subscription["unackedMessages"] for subscription in subscriptions.values()
    )
    guardrails_passed = (
        normalized["storageSize"] == 0
        and backlog == 0
        and normalized["offloadedStorageSize"] == 0
        and normalized["replicationBacklog"] == 0
        and subscription_backlog == 0
        and unacked == 0
        and internal["numberOfEntries"] == 0
        and internal["totalSize"] == 0
        and internal["pendingAddEntriesCount"] == 0
        and internal["compactedLedgerEntries"] == 0
        and internal["compactedLedgerSize"] == 0
        and internal["offloadedManagedLedgers"] == 0
    )
    publisher_count = len(normalized["publishers"])
    if not guardrails_passed:
        status = "blocked_non_empty"
    elif publisher_count:
        status = "candidate_needs_producer_stop_and_revalidation"
    else:
        status = "candidate_needs_owner_confirmation_and_revalidation"
    result.update(
        {
            "status": status,
            "dataGuardrailsPassed": guardrails_passed,
            "storageSize": normalized["storageSize"],
            "backlogSize": backlog,
            "offloadedStorageSize": normalized["offloadedStorageSize"],
            "replicationBacklog": normalized["replicationBacklog"],
            "subscriptionMsgBacklog": subscription_backlog,
            "unackedMessages": unacked,
            "partitionCount": normalized["partitionCount"] or partition_count,
            "publisherCount": publisher_count,
            "publishers": [
                publisher_summary(publisher)
                for publisher in normalized["publishers"]
            ],
            "subscriptions": subscriptions,
            "internalStats": internal,
        }
    )
    return result


def write_actions(
    path: Path,
    results: list[dict[str, Any]],
    expected_quorum: dict[str, int],
) -> None:
    lines = [
        "# RF=1 schema repair actions",
        "",
        "> Read-only report. No producer was stopped and no schema or ledger was modified.",
        "",
        "## Current classification",
        "",
        "| Schema | Current ledger/RF | RF=1 referenced | Status | Storage | Backlog | Unacked | Publishers |",
        "|---|---|---:|---|---:|---:|---:|---:|",
    ]
    for result in results:
        lines.append(
            "| `{schema}` | `{current}/{rf}` | `{ledgers}` | `{status}` | {storage} | {backlog} | "
            "{unacked} | {publishers} |".format(
                schema=result["schemaId"],
                current=result.get("currentLedgerId", "-"),
                rf=result.get("currentLedgerRf", "-"),
                ledgers=",".join(
                    str(ledger_id)
                    for ledger_id in result["referencedRf1LedgerIds"]
                )
                or "-",
                status=result["status"],
                storage=result.get("storageSize", "-"),
                backlog=result.get("backlogSize", "-"),
                unacked=result.get("unackedMessages", "-"),
                publishers=result.get("publisherCount", "-"),
            )
        )
    lines.extend(
        [
            "",
            "## Actions requiring operator coordination",
            "",
            "For each `candidate_*` schema, one at a time:",
            "",
            "1. Identify the owning producer/connector from the publisher evidence and deployment configuration.",
            "2. Agree on a maintenance window and stop that producer safely.",
            "3. Re-run the inventory and then this validator only for that schema; require storage, offloaded storage, backlog, replication backlog, unacked, compacted data and publishers to all be zero.",
            "4. Back up the schema definition and schema metadata.",
            "5. Obtain explicit approval before deleting or recreating anything.",
            "6. After explicit approval, physically delete the schema with `force=true`, restart its producer and verify that the new ledger matches the configured target `E/Qw/Qa={}/{}/{}`. A normal delete only appends a tombstone and retains the RF=1 ledgers.".format(
                expected_quorum["ensembleSize"],
                expected_quorum["writeQuorumSize"],
                expected_quorum["ackQuorumSize"],
            ),
            "7. Verify publishing, consuming, CDC and `listunderreplicated` before moving to the next schema.",
            "",
            "Do not act on `blocked_non_empty`, `blocked_stats_error` or `orphan_only_not_fix2` entries.",
        ]
    )
    path.write_text("\n".join(lines) + "\n")


def main() -> None:
    args = parse_args()
    inventory = json.loads(args.inventory.read_text())
    kubectl = Kubectl(args.namespace, args.context)
    context = kubectl.current_context()
    if inventory.get("context") != context:
        raise AuditError(
            f"Inventory context {inventory.get('context')!r} does not match {context!r}"
        )
    if inventory.get("namespace") != args.namespace:
        raise AuditError("Inventory namespace does not match the requested namespace")
    if inventory.get("release") != args.release:
        raise AuditError("Inventory release does not match the requested release")
    generated_at = datetime.fromisoformat(inventory["generatedAt"])
    max_age = timedelta(seconds=args.max_inventory_age_seconds)
    if datetime.now(UTC) - generated_at > max_age:
        raise AuditError(
            "Inventory is stale; regenerate it immediately before validation"
        )
    if generated_at - datetime.now(UTC) > timedelta(seconds=60):
        raise AuditError("Inventory timestamp is unexpectedly in the future")
    expected_quorum = inventory.get("expectedSchemaLedgerQuorum", {})
    if (
        set(expected_quorum) != {"ensembleSize", "writeQuorumSize", "ackQuorumSize"}
        or min(expected_quorum.values()) < 2
    ):
        raise AuditError(
            f"Unsafe or unknown schema ledger quorum target: {expected_quorum}"
        )
    proxy_service = f"service/{args.release}-proxy"
    selected = set(args.schemas or [])
    by_schema: dict[str, list[dict[str, Any]]] = {}
    for ledger in inventory["ledgers"]:
        if ledger.get("ensembleSize") != 1:
            continue
        schema_id = ledger["schemaId"]
        if selected and schema_id not in selected:
            continue
        by_schema.setdefault(schema_id, []).append(ledger)
    schema_metadata = {
        schema["schemaId"]: schema for schema in inventory.get("schemas", [])
    }
    ledger_rf = {
        ledger["ledgerId"]: ledger.get("ensembleSize")
        for ledger in inventory["ledgers"]
    }

    print(f"Kubernetes context: {context}")
    print(f"Namespace: {args.namespace}")
    print(f"Pulsar admin endpoint: {proxy_service}:80")
    print("Mode: read-only (HTTP GET only)")

    with PortForward(
        kubectl, proxy_service, 80, "/admin/v2/tenants"
    ) as proxy_forward:
        results = [
            validate_schema(
                proxy_forward.base_url,
                schema_id,
                by_schema[schema_id],
                (
                    schema_metadata.get(schema_id, {}).get("current") or {}
                ).get("ledgerId"),
                ledger_rf.get(
                    (
                        schema_metadata.get(schema_id, {}).get("current") or {}
                    ).get("ledgerId")
                ),
                args.http_timeout,
            )
            for schema_id in sorted(by_schema)
        ]

    summary = {
        "schemasChecked": len(results),
        "dataGuardrailsPassed": sum(
            result["dataGuardrailsPassed"] for result in results
        ),
        "needsProducerStop": sum(
            result["status"] == "candidate_needs_producer_stop_and_revalidation"
            for result in results
        ),
        "needsOwnerConfirmation": sum(
            result["status"]
            == "candidate_needs_owner_confirmation_and_revalidation"
            for result in results
        ),
        "blockedNonEmpty": sum(
            result["status"] == "blocked_non_empty" for result in results
        ),
        "blockedStatsError": sum(
            result["status"] == "blocked_stats_error" for result in results
        ),
        "blockedIncompletePartitionStats": sum(
            result["status"] == "blocked_incomplete_partition_stats"
            for result in results
        ),
        "blockedUnknownReference": sum(
            result["status"] == "blocked_unknown_reference" for result in results
        ),
        "blockedNonPersistentTopic": sum(
            result["status"] == "blocked_non_persistent_topic_exists"
            for result in results
        ),
        "orphanOnly": sum(
            result["status"] == "orphan_only_not_fix2" for result in results
        ),
    }
    report = {
        "generatedAt": datetime.now(UTC).isoformat(),
        "context": context,
        "namespace": args.namespace,
        "release": args.release,
        "readOnly": True,
        "sourceInventory": str(args.inventory),
        "expectedSchemaLedgerQuorum": expected_quorum,
        "summary": summary,
        "results": results,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_json(args.output_dir / "schema-candidate-validation.json", report)
    write_actions(args.output_dir / "actions.md", results, expected_quorum)
    with (args.output_dir / "schema-candidate-validation.csv").open(
        "w", newline=""
    ) as output:
        fields = [
            "schemaId",
            "referencedRf1LedgerIds",
            "unreferencedRf1LedgerIds",
            "unknownReferenceRf1LedgerIds",
            "currentLedgerId",
            "currentLedgerRf",
            "currentLedgerIsRf1",
            "status",
            "dataGuardrailsPassed",
            "storageSize",
            "backlogSize",
            "offloadedStorageSize",
            "replicationBacklog",
            "subscriptionMsgBacklog",
            "unackedMessages",
            "publisherCount",
            "partitionCount",
        ]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    **{field: result.get(field) for field in fields},
                    "referencedRf1LedgerIds": ",".join(
                        str(value) for value in result["referencedRf1LedgerIds"]
                    ),
                    "unreferencedRf1LedgerIds": ",".join(
                        str(value)
                        for value in result["unreferencedRf1LedgerIds"]
                    ),
                    "unknownReferenceRf1LedgerIds": ",".join(
                        str(value)
                        for value in result["unknownReferenceRf1LedgerIds"]
                    ),
                }
            )

    print(f"Validation written to {args.output_dir}")
    for key, value in summary.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    try:
        main()
    except (AuditError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"ERROR: {error}") from error
