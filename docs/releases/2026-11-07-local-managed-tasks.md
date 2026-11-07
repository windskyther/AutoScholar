# Local access, managed models and research task types

The local workbench and business API no longer require a user token. The legacy
experiment REST router and access module have been removed. Host/Origin checks,
request limits, audit logging and sandbox/MCP service isolation remain.

Saved model keys use a separate `MODEL_ENCRYPTION_KEY`; it is never used as an
HTTP credential. Preserve the previous encryption material under this setting
when upgrading. All language models must be added in model management and
selected for calls/tasks. Environment model settings cannot supply a fallback.

New task types are research, literature review, technical comparison, research
proposal and project document analysis. Each type supplies its planning,
execution and review requirements. Literature review defaults to paper search.
Document analysis requires ready project PDFs and freezes their selected IDs.
New fixed-training tasks are rejected. Historical records and internal execution
components remain readable without rewriting earlier results.

Verification used no paid provider calls:

- Focused backend regression: 125 passed; one Linux ownership check skipped on Windows.
- Extended facade/control/resource regression: 283 passed; an obsolete test fixture
  decorator was then restored and its module passed in the focused regression.
- Frontend: 113 unit tests passed, TypeScript and production build passed.
- Mocked browser coverage passed across task forms, local access, projects,
  deletion, export, events, usage and navigation; final affected task-type checks passed.
- Four real browser tests against an isolated SQLite backend passed, including
  model persistence, consent, budget controls and selected-model submission;
  provider and sandbox dispatch counts remained zero.
- Ruff and mypy passed. Existing local model credentials decrypted with the
  migrated encryption configuration without exposing their values.

The historical scripted paired-evaluation test also fails on the unchanged
`3ae4b12` baseline. It is recorded as pre-existing, not reclassified as passing.
Local runtime replacement requires separate authorization; Git integration alone
does not restart running services or declare a new Phase accepted.
