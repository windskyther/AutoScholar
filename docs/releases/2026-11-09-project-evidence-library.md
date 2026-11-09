# Project evidence library

New research goals can reuse audited evidence within their project. Selection runs
before planning, freezes original source versions in the task checkpoint, and respects
source types and the selected PDF scope. Independent screening, writing and review
still examine original passages. Relevant fresh evidence permits zero to six external
queries; latest/current goals still require new searches. The offline A → B fixture
reduces external queries from three to zero without bypassing quality review.

The project page now links to an evidence browser with source links and quotations,
contributing goals, reuse counts, filters, withdrawal/restoration, recheck settings and
explicit paginated history import. The task form defaults reuse on when the backend
supports it. Task details show selection/adoption receipts. Older backends and task
checkpoints retain their previous behavior.

Web/paper recheck intervals default to 7/30 days and are configurable per project.
Reuse does not renew acquisition timestamps. Frozen fresh sources that expire during
a long pause stop the task before another model call. Withdrawal, deleted origins,
changed PDFs and cyclic or invalid provenance prevent reuse, including derived claims.
Calls already returned retain their actual token usage when evidence is revoked.
Failed/cancelled goals close unused selection receipts without publishing new claims.

PostgreSQL remains authoritative. The existing workflow worker publishes through the
durable outbox; the document worker maintains a separate Qdrant collection. Evidence
vectors use an independent local-only loader for existing cached weights. Missing
models/index outages fall back to keywords without downloading models or invoking
paid embedding APIs. Collection queries filter project, model and index version.

Migration 0021 adds source-type metadata and per-project recheck policy, following
0020's independent evidence tables. History import queues fully audited successes;
other legacy evidence remains pending review and is not automatically reusable.
An explicit import can retry an exhausted publication outbox. Historical reports and
evaluation outcomes are unchanged.

Validation covers SQLite lifecycle and API contracts, real isolated PostgreSQL
migration round trips/publication races, real isolated Qdrant operations with
deterministic local vectors, mocked generic/DeepSeek SDK codecs, browser interactions,
frontend build/unit checks and Python lint/type checks. The existing managed-model
HTTP fixture was corrected to distinguish pricing rows from model rows.

No paid model, retrieval or embedding call, live schema migration, or deployment was
performed. To use this version locally, rebuild/restart the backend (including its
migration service and workers) and refresh the frontend. Preserve database volumes
and the existing local model-encryption key. Live-provider comparison is a separately
authorized optional follow-up.
