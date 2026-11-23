# Phase 11C: request governance and audit

Authenticated API processes now share atomic Redis rate limits and expiring concurrency
leases. Body and response caps, upload deadlines, route-specific execution deadlines
and SSE limits protect the existing task budgets and sandbox controls. Defaults and
operator recovery rules are in [the governance guide](../security/governance.md).

Migration `20261104_0014` adds append-only PostgreSQL security audit. Startup records
a policy fingerprint; requests record admission before dispatch and mutations commit
completion audit before returning success. An audit outage blocks new work. A failed
completion commit reports an unknown outcome and retains admission evidence, so the
client must inspect the original operation rather than blindly resend it. External
operation audit complements the existing Journal; pending calls and historical
evaluation outcomes retain their meaning.

LLM HTTP retries remain disabled. Research HTTP retries now also default to zero;
an explicit adapter override has capped attempts/backoff and shares the original
search deadline. Structured logs redact configured secrets, sensitive fields, URLs,
foreign HTTP-client diagnostics and arbitrary exception tracebacks. Request logging
uses route templates instead of raw paths and query strings.

Validation uses public fixtures without LLM, Tavily or Semantic Scholar calls:

- 129 focused API/security/Journal regression checks passed; a final 64-case security
  selection passed after policy and startup changes. These selections overlap.
- Three disposable-service checks passed: two independent Python processes sharing
  Redis admission, the complete PostgreSQL migration chain and new-revision roundtrip,
  and HTTP requests using real Redis/PostgreSQL with a fake supplier. The second API
  instance was denied without a supplier dispatch; successful usage was persisted.
- Ten real local browser cases passed: PDF/document writes and idempotent lost-receipt
  recovery (3), SSE authentication/cursor reconnection (3), and approval/control
  concurrency and lost acknowledgements (4).
- Ruff, strict mypy, Actionlint and Compose default/custom-limit parsing passed.
  Hosted Checks also runs full Linux regression with disposable Redis/PostgreSQL,
  targeted Windows compatibility and frontend checks.

Offline HTTP fixtures explicitly inject stores that implement the admission contract;
the application has no implicit test-mode bypass. A production-factory regression
confirms missing governance infrastructure fails closed. Temporary service containers
are ownership checked and removed after testing. No normal installation was migrated,
no application was deployed, and no credentials, design documents or runtime data
are included in this release.

This module does not accept all of Phase 11. Sandbox control-plane hardening,
production ingress/images, release gates, backup/restore and final acceptance
(11D–11H) remain separate work.
