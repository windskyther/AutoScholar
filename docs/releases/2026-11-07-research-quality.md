# Research quality passes

New public research tasks default to standard depth. Quick preserves the previous
workflow; historical checkpoints with no depth remain quick and keep their original
budgets and results. Deep permits two bounded repair rounds; standard permits one.

The quality stage collects all persisted search candidates (not only child-selected
evidence), deduplicates URLs/DOIs/arXiv versions, screens candidates, optionally reads
selected source text, independently rescreens all candidates, writes a unified report,
and audits its claims. Reviews can request targeted revisions or supplemental searches.
Every pass uses the same native tool schema. Frozen source JSON and the system policy
form a stable prefix; the second selector does not receive the first selector's reasons.
Changed material creates a new bounded snapshot. Cache hits are best effort, recorded
from provider usage, and never exempt from total-token accounting.

Tavily Extract uses the configured native research backend and existing search/tool
budgets. It can consume provider credits. Extraction is bounded, cached locally, and
never fetched from arbitrary URLs by the local server. Missing credentials, failed
URLs and MCP backends retain honest summary-only labels. Local document expansion is
restricted to selected, ready documents in the current project.

Quotes must exist in the frozen text; source IDs and dates are server-validated. This
checks origin and temporal eligibility, not a mathematical guarantee of semantic truth.
Independent audit assesses support, conflicts, coverage and limitations. Unresolved
review failures retain an explicitly unverified draft. Completed quality passes are
checkpointed; uncertain interrupted external calls stop for recovery instead of replay.

Final evidence records are published on the root task with the report's global E IDs.
The resource browser uses those authoritative records, retaining raw child traces for
diagnosis. Writer replay is idempotent. The read-only quality panel is collapsed by
default and projects source links, access limits, screening reasons and review issues.

Validation is offline with synthetic models, SQLite and mocked extraction. No real
model/Tavily calls, historical outcome changes or runtime migrations are included.
The frontend hides new controls against a backend without the advertised capability.
