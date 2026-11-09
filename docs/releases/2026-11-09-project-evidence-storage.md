# Project evidence storage foundation

Successful, quality-audited research workflows enqueue publication in the same
transaction as their final checkpoint. The existing workflow worker publishes bounded
source versions, supported claims and provenance without model or retrieval calls.
Publication is idempotent and retryable; a publication failure does not change a
successful task, its report or historical evaluation outcomes.

Sources deduplicate within their project by identity and content/metadata hash. Task
topics, local citation labels, reuse lineage and retrieval timestamps do not create
duplicate source content. Exact quotations remain bound to the published report.
Deleted tasks/projects, invalid provenance and unavailable documents are excluded.
Acquisition times are recorded on real retrieval and preserved across cache hits;
unknown historical acquisition times remain unknown.

Migration 0020 adds independent source, claim, publication and reuse tables without
backfilling or changing old task outcomes. Offline SQLite tests cover publication,
redelivery, source tampering, deletion before/after publishing and migration round trips.
Existing quality, retrieval and migration regressions also passed. This is the storage
module only; cross-task selection, the user interface and full PostgreSQL/Qdrant
acceptance remain separate work. No paid calls or live migration was performed.
