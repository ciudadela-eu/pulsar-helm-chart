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
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from pulsar_audit_common import (
    AuditError,
    HttpError,
    Kubectl,
    PortForward,
    decode_custom_metadata,
    http_json,
    schema_parts,
    write_json,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a read-only inventory of Pulsar schema ledgers."
    )
    parser.add_argument("--namespace", default="pulsar")
    parser.add_argument("--release", default="pulsar")
    parser.add_argument("--context")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--http-timeout", default=30, type=int)
    return parser.parse_args()


def schema_metadata_url(proxy_url: str, schema_id: str) -> str:
    tenant, namespace, topic = schema_parts(schema_id)
    return (
        f"{proxy_url}/admin/v2/schemas/{quote(tenant, safe='')}/"
        f"{quote(namespace, safe='')}/{quote(topic, safe='')}/metadata"
    )


def main() -> None:
    args = parse_args()
    kubectl = Kubectl(args.namespace, args.context)
    context = kubectl.current_context()
    bookie = kubectl.ready_pod(f"release={args.release},component=bookie")
    proxy_service = f"service/{args.release}-proxy"

    print(f"Kubernetes context: {context}")
    print(f"Namespace: {args.namespace}")
    print(f"BookKeeper metadata endpoint: pod/{bookie}:8000")
    print(f"Pulsar admin endpoint: {proxy_service}:80")
    print("Mode: read-only (HTTP GET only)")

    with PortForward(
        kubectl, f"pod/{bookie}", 8000, "/api/v1/config/server_config"
    ) as bookie_forward, PortForward(
        kubectl, proxy_service, 80, "/admin/v2/tenants"
    ) as proxy_forward:
        ledgers_payload = http_json(
            f"{bookie_forward.base_url}/api/v1/ledger/list"
            "?print_metadata=true&decode_meta=true",
            timeout=max(args.http_timeout, 120),
        )

        ledgers = []
        schema_ids = set()
        for ledger_id, raw_metadata in ledgers_payload.items():
            if not isinstance(raw_metadata, dict):
                continue
            custom_metadata = decode_custom_metadata(
                raw_metadata.get("customMetadata", {})
            )
            if custom_metadata.get("component") != "schema":
                continue
            schema_id = custom_metadata.get("pulsar/schemaId")
            if not schema_id:
                continue
            schema_ids.add(schema_id)
            ledgers.append(
                {
                    "ledgerId": int(ledger_id),
                    "schemaId": schema_id,
                    "ensembleSize": raw_metadata.get("ensembleSize"),
                    "writeQuorumSize": raw_metadata.get("writeQuorumSize"),
                    "ackQuorumSize": raw_metadata.get("ackQuorumSize"),
                    "state": raw_metadata.get("state"),
                    "length": raw_metadata.get("length"),
                    "lastEntryId": raw_metadata.get("lastEntryId"),
                    "ctime": raw_metadata.get("ctime"),
                    "customMetadata": custom_metadata,
                    "referenced": None,
                    "schemaVersions": [],
                }
            )

        schemas = []
        references: dict[tuple[str, int], list[int]] = {}
        for schema_id in sorted(schema_ids):
            try:
                metadata = http_json(
                    schema_metadata_url(proxy_forward.base_url, schema_id),
                    timeout=args.http_timeout,
                )
                index = metadata.get("index", [])
                for entry in index:
                    ledger_id = entry.get("ledgerId")
                    version = entry.get("version")
                    if ledger_id is not None:
                        references.setdefault((schema_id, int(ledger_id)), []).append(
                            int(version) if version is not None else -1
                        )
                schemas.append(
                    {
                        "schemaId": schema_id,
                        "status": "ok",
                        "current": metadata.get("info"),
                        "index": index,
                    }
                )
            except HttpError as error:
                schemas.append(
                    {
                        "schemaId": schema_id,
                        "status": "error",
                        "httpStatus": error.status,
                        "error": error.body[:1000],
                        "current": None,
                        "index": [],
                    }
                )

        runtime_config = http_json(
            f"{proxy_forward.base_url}/admin/v2/brokers/configuration/runtime",
            timeout=args.http_timeout,
        )
        expected_quorum = {
            "ensembleSize": int(runtime_config["managedLedgerDefaultEnsembleSize"]),
            "writeQuorumSize": int(
                runtime_config["managedLedgerDefaultWriteQuorum"]
            ),
            "ackQuorumSize": int(runtime_config["managedLedgerDefaultAckQuorum"]),
        }
        schema_status = {schema["schemaId"]: schema["status"] for schema in schemas}
        for ledger in ledgers:
            key = (ledger["schemaId"], ledger["ledgerId"])
            if schema_status[ledger["schemaId"]] == "ok":
                ledger["referenced"] = key in references
                ledger["schemaVersions"] = sorted(references.get(key, []))

        ledgers.sort(key=lambda ledger: (ledger["schemaId"], ledger["ledgerId"]))
        rf1_ledgers = [ledger for ledger in ledgers if ledger["ensembleSize"] == 1]
        summary = {
            "totalBookKeeperLedgers": len(ledgers_payload),
            "schemaLedgers": len(ledgers),
            "schemas": len(schema_ids),
            "schemaMetadataErrors": sum(
                schema["status"] != "ok" for schema in schemas
            ),
            "rf1SchemaLedgers": len(rf1_ledgers),
            "rf1Referenced": sum(
                ledger["referenced"] is True for ledger in rf1_ledgers
            ),
            "rf1Unreferenced": sum(
                ledger["referenced"] is False for ledger in rf1_ledgers
            ),
            "rf1UnknownReference": sum(
                ledger["referenced"] is None for ledger in rf1_ledgers
            ),
            "schemasWithRf1": len(
                {ledger["schemaId"] for ledger in rf1_ledgers}
            ),
        }
        report = {
            "generatedAt": datetime.now(UTC).isoformat(),
            "context": context,
            "namespace": args.namespace,
            "release": args.release,
            "readOnly": True,
            "expectedSchemaLedgerQuorum": expected_quorum,
            "summary": summary,
            "schemas": schemas,
            "ledgers": ledgers,
        }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_json(args.output_dir / "schema-ledger-inventory.json", report)
    with (args.output_dir / "schema-ledger-inventory.csv").open(
        "w", newline=""
    ) as output:
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "ledgerId",
                "schemaId",
                "schemaVersions",
                "referenced",
                "ensembleSize",
                "writeQuorumSize",
                "ackQuorumSize",
                "state",
                "length",
                "lastEntryId",
                "ctime",
            ],
        )
        writer.writeheader()
        for ledger in ledgers:
            writer.writerow(
                {
                    **{key: ledger.get(key) for key in writer.fieldnames},
                    "schemaVersions": ",".join(
                        str(version) for version in ledger["schemaVersions"]
                    ),
                }
            )

    print(f"Inventory written to {args.output_dir}")
    for key, value in summary.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    try:
        main()
    except (AuditError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"ERROR: {error}") from error
