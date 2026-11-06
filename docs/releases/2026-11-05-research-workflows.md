# Research workflows and source verification

Workbench tasks default to **资料调研**. An explicit **MNIST CPU 实验** selection
retains the existing experiment workflow. The backend advertises
`research_workflows`; clients without that capability retain the experiment path.

Autonomous requests can set `workflow_kind=research`. The server requires the plan
goal to equal the original objective and rejects coding/experiment steps during
both initial planning and revision. Research reports preserve reviewed answers
and globally numbered source references without an extra model call. Existing API
requests and checkpoints default to `experiment` for compatibility.

An empty `document_ids` list disables both PDF retrieval and document coverage
requirements. For durable research, a null selection is resolved to the currently
ready documents during submission; an empty library becomes an explicit empty
selection. Project locking prevents enqueueing work after project deletion.

Source verification compares excerpts with persisted search receipts or matching
same-project document chunks. It does not require a paraphrased claim to be a
literal substring. Semantic claim support remains a separate review obligation;
valid source quotations cannot force unsupported claims to pass. Research-only
workflows also reject unverified source quotations deterministically.

Planning, query generation and reporting receive a server UTC date and an explicit
month range for Chinese year/month objectives. This supplies temporal context;
publication dates still need source verification, and this change does not claim
hard publication-date filtering by every search provider.

Model request timeout defaults to 180 seconds, configurable up to 600. Explicit
existing configuration takes precedence. Unknown calls retain their journals and
remain stopped without automatic retry; the workbench exposes the underlying
step error alongside the recovery state. Changing timeout does not recover an
already unknown call or reset its usage.

Task controls and resource panels now use distinct component identities. Repeated
overview refreshes no longer duplicate the approval/control section; browser
regression coverage includes successive refreshes with both panels enabled.

Old checkpoint payloads remain readable and are not rewritten by this change.
Frozen v6/v7 evaluation contracts retain their original stage-contract digest;
current quotation rules do not alter historical protocol identities or grades.
Older application versions cannot read new checkpoint fields; do not roll back
workers over newly submitted research workflows without a compatible recovery
plan. No database migration or historical evaluation rescoring is required.

Regression tests use scripted models, public source fixtures and local databases.
They cover research without training, restart, invalid scope, disabled PDFs,
tampered sources, semantic review, unknown-call protection and the browser flow.
They do not substitute for a separately authorized real-provider rerun.
