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

import unittest
from unittest.mock import patch

from pulsar_audit_common import HttpError, decode_custom_metadata
from validate_schema_candidates import normalize_internal_stats, normalize_stats
from validate_schema_candidates import validate_schema


class SchemaAuditTest(unittest.TestCase):
    def test_decodes_bookkeeper_custom_metadata(self) -> None:
        self.assertEqual(
            decode_custom_metadata(
                {
                    "component": "c2NoZW1h",
                    "pulsar/schemaId": "cHVibGljL3NpbmtzL19fY2hhbmdlX2V2ZW50cw==",
                }
            ),
            {
                "component": "schema",
                "pulsar/schemaId": "public/sinks/__change_events",
            },
        )

    def test_normalizes_partitioned_stats(self) -> None:
        stats = normalize_stats(
            {
                "partitions": {
                    "p-0": {
                        "storageSize": 10,
                        "backlogSize": 2,
                        "offloadedStorageSize": 3,
                        "publishers": [{"producerName": "one"}],
                        "subscriptions": {
                            "sub": {"msgBacklog": 1, "unackedMessages": 4}
                        },
                        "replication": {"remote": {"replicationBacklog": 5}},
                    },
                    "p-1": {
                        "storageSize": 20,
                        "backlogSize": 0,
                        "offloadedStorageSize": 0,
                        "publishers": [],
                        "subscriptions": {
                            "sub": {"msgBacklog": 2, "unackedMessages": 0}
                        },
                        "replication": {},
                    },
                }
            }
        )
        self.assertEqual(stats["storageSize"], 30)
        self.assertEqual(stats["backlogSize"], 2)
        self.assertEqual(stats["offloadedStorageSize"], 3)
        self.assertEqual(stats["replicationBacklog"], 5)
        self.assertEqual(stats["subscriptions"]["sub"]["msgBacklog"], 3)
        self.assertEqual(stats["subscriptions"]["sub"]["unackedMessages"], 4)

    def test_normalizes_internal_stats(self) -> None:
        stats = normalize_internal_stats(
            [
                {
                    "numberOfEntries": 2,
                    "totalSize": 100,
                    "pendingAddEntriesCount": 1,
                    "ledgers": [{"offloaded": True}],
                    "compactedLedger": {"entries": 3, "size": 50},
                },
                {
                    "numberOfEntries": 1,
                    "totalSize": 25,
                    "pendingAddEntriesCount": 2,
                    "ledgers": [{"offloaded": False}],
                    "compactedLedger": {"entries": -1, "size": -1},
                },
            ]
        )
        self.assertEqual(stats["numberOfEntries"], 3)
        self.assertEqual(stats["totalSize"], 125)
        self.assertEqual(stats["pendingAddEntriesCount"], 3)
        self.assertEqual(stats["offloadedManagedLedgers"], 1)
        self.assertEqual(stats["compactedLedgerEntries"], 3)
        self.assertEqual(stats["compactedLedgerSize"], 50)

    def test_blocks_unknown_schema_reference_without_http_calls(self) -> None:
        with patch("validate_schema_candidates.http_json") as http:
            result = validate_schema(
                "http://localhost",
                "public/default/topic",
                [{"ledgerId": 1, "ensembleSize": 1, "referenced": None}],
                1,
                1,
                1,
            )
        http.assert_not_called()
        self.assertEqual(result["status"], "blocked_unknown_reference")

    def test_blocks_incomplete_partition_stats(self) -> None:
        with patch(
            "validate_schema_candidates.http_json",
            side_effect=[
                {"partitions": 2},
                {"partitions": {"persistent://public/default/topic-partition-0": {}}},
                HttpError("http://localhost", 404, "not found"),
            ],
        ):
            result = validate_schema(
                "http://localhost",
                "public/default/topic",
                [{"ledgerId": 1, "ensembleSize": 1, "referenced": True}],
                1,
                1,
                1,
            )
        self.assertEqual(result["status"], "blocked_incomplete_partition_stats")
        self.assertEqual(result["expectedPartitions"], 2)
        self.assertEqual(result["observedPartitions"], 1)


if __name__ == "__main__":
    unittest.main()
