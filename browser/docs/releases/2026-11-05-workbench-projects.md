# Workbench project management

The workbench removes development-phase labels, backend names, version badges and
implementation notes from its normal screens. Connection status, execution errors,
budgets, approval consequences and explicit paid-task consent remain visible.

Project deletion is available in both the list and detail views. The confirmation
requires the project's exact name and prevents duplicate clicks. If a request's
outcome is uncertain, the dialog checks the project before allowing another action.

`DELETE /projects/{id}` and its `/workbench` facade require the existing access
credential. A successful or repeated deletion returns 204; an unknown project
returns 404. Unfinished tasks, uncertain invocation receipts and queued/processing
documents return 409. The API advertises `project_deletion` so an older backend
does not expose an unusable button.

Deletion records `projects.deleted_at` and removes the project from lists, counts
and workbench resource views. It does not erase task results, invocation journals,
PDFs, vectors or experiment workspaces. This preserves traceability and prevents
deletion from silently resetting paid-call history. Project locking also prevents
new tasks or document jobs from being attached after deletion. There is no restore
button in this version.

Run `alembic upgrade head` before starting this code; migration `20261105_0015`
adds the nullable deletion timestamp without deleting existing records. Docker
Compose performs the migration during startup. Database/image compatibility must
be checked before rolling back to an older migration head.

Verification covers authenticated and repeated deletion, retained histories,
deleted-resource visibility, unfinished tasks and descendants, unknown calls,
document jobs, confirmation/cancellation, uncertain responses and another project's
continued visibility. Tests use public fixtures without supplier API calls.

Local cleanup of the owner's Phase 3/7 acceptance projects is recorded only under
ignored `data/validation/`; private project metadata and database backups are not
part of the source commit.
