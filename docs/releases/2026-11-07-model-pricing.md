# Optional model cost estimates

Model creation accepts optional CNY or USD rates per million input, cached input and
output tokens. Rates are immutable with the connection and excluded from credentials.
Leaving prices unset retains unknown costs. A missing cached rate means a uniform
input rate; differential cache rates require complete, consistent cache usage.
Reasoning tokens stay included in output and are never billed a second time here.

Each dispatched model call saves its price snapshot. Estimates use that snapshot,
never the latest configuration; old rows remain unpriced. CNY and USD aggregate into
separate amounts across API/model/date/stage filters, with incomplete-call counts.
This is a model-only estimate, excluding retrieval charges and account-specific
billing adjustments. It is not a provider invoice or an authorization to spend.

Migration 0019 adds an optional price table and nullable per-call metadata without
changing historical outcomes, token counts or model-profile rows. Offline tests cover
missing cache splits, zero hits, full hits, malformed totals, immutable/idempotent
creation, frozen prices, mixed currencies, unknowns and upgrade/downgrade/upgrade.
Browser fixtures exercise price entry and estimates without contacting a provider.
No live migration, container restart or paid API calls are part of this integration.

Verification: 251 related backend regressions passed before the final citation-export
hardening; the resulting quality/pricing/ledger/migration subset passed 58 tests.
Both SDK compatibility profiles passed, all 335 Python files passed type checking,
the frontend built, and 113 frontend unit tests passed. Price entry and ten other
quality/usage browser scenarios passed with network fixtures; earlier task submission
browser coverage remains applicable to the capability-gated depth control.
