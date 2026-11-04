# Phase 11E: production packaging and ingress

An independent production Compose file now builds a non-root API/worker image and
a static Web image served by Nginx. Only the loopback Web ingress publishes a host
port. Internal application services, data stores and the separately authenticated
Docker manager have no host ports. The manager remains a trusted root/socket
exception; application and Web containers have read-only roots and explicit writable
mounts, resource bounds, health probes and graceful-stop limits.

The proxy retains `/api/workbench` semantics, genuine API 404 responses, authenticated
uploads/downloads, SSE heartbeats and cursor reconnects. Forwarded identity headers
are stripped, API proxy-header trust stays disabled, and failed upstream mutations
are not replayed. A separate TLS overlay replaces the HTTP port; HTTPS browser writes
require an explicitly configured origin. TLS acceptance uses a temporary localhost
certificate, not a public deployment or certificate-chain validation.

Images record their source commit and lock digests; the backend also records the
unique migration head. Fresh secret setup refuses to overwrite existing files. New
volume initialization refuses unmarked legacy content and never recursively changes
ownership. A Linux copy check verifies the original bytes and owner remain unchanged
while a separate cache copy becomes writable by the application user. Native Windows
workers do not run the Linux health probe or signal a process to check its liveness.

Isolated HTTP and HTTPS browser checks use real PostgreSQL, Redis, Qdrant, PDF parsing
and document-worker indexing, with deterministic public vectors and a workflow worker
held idle. They cover session connect/disconnect, Chinese text, PDF transfer, report
preview/export, SSE delivery and reconnection, durable idempotent submission and
approval/cancellation. No supplier credentials or calls are involved. Failed fixture
attempts are retained separately; successful stacks remove their own containers and
volumes. Application/static-image checks compare local secrets in memory and exclude
environment files, private designs and runtime data from packaged project material.

See [the production operating guide](../operations/production.md) for configuration,
builds and repeatable isolated checks. This module does not deploy the normal app,
alter historical evaluation outcomes, or accept Phase 11 as a whole. Full supply-chain
release gates, cross-store backup/restore and upgrade/rollback, and the final CPU
workflow-stack acceptance remain in Phase 11F–11H.
