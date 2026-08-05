# Schema ledger RF=2 migration audit

These read-only scripts were created for the production migration of legacy
Pulsar schema ledgers from RF=1 to `E/Qw/Qa=2/2/2`. They inventory BookKeeper
schema ledgers and validate the data-safety guardrails required before an
operator recreates a schema.

Python 3.11 or newer is required. The validator accepts only inventories from
the same context, namespace and release that are less than 15 minutes old.

They only use `kubectl get`, `kubectl port-forward`, and HTTP GET requests. They
do not stop producers or mutate topics, schemas, ledgers, or BookKeeper metadata.

The inventory performs one cluster-wide BookKeeper metadata read. The production
cluster used for this workflow currently has hundreds of ledgers and produces a
small response; assess BookKeeper load before using it on substantially larger
clusters.

## Inventory

```bash
python3 scripts/pulsar/schema-ledger-rf2-migration/audit_schema_ledgers.py \
  --namespace pulsar \
  --release pulsar \
  --output-dir /tmp/pulsar-schema-audit
```

Outputs:

- `schema-ledger-inventory.json`: complete machine-readable report.
- `schema-ledger-inventory.csv`: ledger-level review table.

## Guardrail validation

```bash
python3 scripts/pulsar/schema-ledger-rf2-migration/validate_schema_candidates.py \
  --namespace pulsar \
  --release pulsar \
  --inventory /tmp/pulsar-schema-audit/schema-ledger-inventory.json \
  --output-dir /tmp/pulsar-schema-audit
```

After stopping a producer, regenerate the inventory and then use
`--schema public/sinks/__change_events` to revalidate only that schema.

Outputs:

- `schema-candidate-validation.json`: stats, subscriptions, and publishers.
- `schema-candidate-validation.csv`: candidate classification table.
- `actions.md`: ordered operator actions. No destructive commands are run.

## Required guardrails

A schema is not eligible for recreation unless all of these are zero in a
fresh validation performed after stopping its producer:

```text
storageSize
offloadedStorageSize
backlogSize
replicationBacklog
subscription.msgBacklog
subscription.unackedMessages
internalStats.numberOfEntries
internalStats.totalSize
internalStats.pendingAddEntriesCount
compactedLedger.entries
compactedLedger.size
publishers
```

Entries classified as `blocked_non_empty`, `blocked_stats_error`, or
`orphan_only_not_fix2` must not be modified.

## Tests

```bash
python3 scripts/pulsar/schema-ledger-rf2-migration/test_schema_audit.py
```

## Schema deletion semantics

The regular Pulsar schema DELETE endpoint appends a tombstone. It does not
remove the existing schema index or its BookKeeper ledgers. Re-registering the
schema after a regular delete creates a new RF=2 current version but leaves the
legacy RF=1 versions referenced.

The migration required physical schema deletion with the REST query parameter
`force=true`, but destructive one-off production commands are intentionally not
automated here. Use `force=true` only after all guardrails pass, the schema and
metadata are backed up, the producer is stopped, and explicit approval has been
obtained. Never use it when a topic retains entries because replay may require
the deleted schema versions.
