# Maintenance and recovery

These commands are local Docker-operator tools. Run from the trusted repository root;
they expose no remote maintenance endpoint. Select the production project explicitly
and use its private configuration/secret directory. Ordinary development containers
and volumes are not backup or restore targets for the acceptance commands.

```powershell
$project = 'autoscholar-prod'
python -m autoscholar.deployment.operations status --project $project
python -m autoscholar.deployment.operations maintenance --project $project
python -m autoscholar.deployment.operations backup --project $project
```

`status` reports service health/OOM state, the last hour's admitted/completed request
latency p95, errors/429s, queue states, expired leases, pending approvals/unknown
invocations and persisted usage totals. Task-specific limits and remaining budgets
remain visible in the authenticated task overview; no prompt or credential is a
metric label. For local host capacity use `docker stats --no-stream` and
`docker system df`; inspect sandbox objects with
`docker ps -a --filter label=autoscholar.sandbox=true` and
`docker volume ls --filter label=autoscholar.sandbox=true`. Stopped/unattached
objects are investigation candidates, not permission to delete another instance's
resources. Ownership checks and task receipts must precede cleanup.

Maintenance stops ingress and application writers/consumers with a graceful deadline;
durable interruption, pending calls and journals are retained. Backup refuses running
writers. It takes a consistent PostgreSQL custom-format dump, stops Redis and Qdrant
cleanly, and archives documents, source/workspaces/artifacts, complete Redis AOF/RDB
and the cold Qdrant storage/WAL. It restarts storage only; consumers stay stopped.
This is an offline maintenance window, not an online cross-store snapshot.

Backups go into new directories under Git-ignored `data/`. Linux permissions or
Windows private ACLs protect each new backup directory. The manifest binds every
archive/dump checksum, schema, PostgreSQL major, image and key database counters.
Backup files contain private application data: never upload them as CI artifacts,
commit them, or serve them through the application. Encryption at rest and a separate
off-device copy are operator storage choices, not automatic properties of this tool.
Secret files and TLS private keys are kept separately; public model caches and the
downloadable MNIST benchmark are prepared separately after recovery.
There is no automatic backup deletion. Choose the backup directory explicitly and
keep the last verified maintenance point until its replacement passes restore;
apply the operator's retention policy only to explicitly selected backup directories.

## Restore to a new project

Choose a **new project with no containers or volumes**. Generate a separate secret
directory with `scripts/configure_production.py`, then set `PROD_SECRETS_DIR` to it.
The PostgreSQL logical dump allows fresh target database credentials; Redis and
Qdrant read their new credentials from the target's secret configuration.

```powershell
$target = 'autoscholar-restored-20261105'
$backup = 'D:\98281\deepscholar\data\backups\SELECT-VERIFIED-BACKUP'
python -m autoscholar.deployment.operations restore --project $target --backup $backup
python -m autoscholar.deployment.operations status --project $target
```

Restore verifies all hashes and archive headers before creating volumes. It rejects
path traversal, links, devices, duplicate paths, malformed ownership and archives
exceeding 200,000 entries or 32 GiB per volume. It preserves directory ownership and
restores PostgreSQL data and original queue/approval/event/artifact records. Redis
limits and lease expirations are preserved rather than replaced with an empty cache.
Qdrant uses the complete cold snapshot. Consumers do not start automatically.

Inspect pending calls, unresolved invocation receipts and leases before resuming.
`resume` refuses unknown operations; no reset or forced resend option is provided.
Use the established reconciliation flow for uncertain tasks, keeping consumers
stopped until their receipts are understood. Operator actions append metadata-only
audit records below `data/deployment/operations/<project>/`.

## Compatible upgrade and rollback

Keep a verified backup from the maintenance point. Supply a locally present,
reviewed image digest or tag explicitly. The tool compares the target image's sealed
migration head with the database; an incompatible schema is refused. Switching an
image additionally requires a checksum-verified backup matching the database.

```powershell
python -m autoscholar.deployment.operations check-switch --project $project --image SELECT-REVIEWED-IMAGE
python -m autoscholar.deployment.operations resume --project $project --image SELECT-REVIEWED-IMAGE --backup $backup
```

For rollback, re-enter maintenance and select the previous compatible image with
the same checks. No command automatically downgrades PostgreSQL or Alembic. If a
schema change is required, rehearse migration on a fresh restored project first;
an incompatible rollback uses the verified backup and a fresh project instead.
The commands do not publish images or select a real deployment target automatically.

The reproducible public-fixture drill is `python scripts/verify_recovery.py`. It
checks HTTP ingress, private backup, tamper rejection, fresh-volume restore, retained
Redis TTL/Qdrant data, compatible image switch/rollback and refusal to resume an
unknown call. It uses no supplier API and removes only its owned test resources.
