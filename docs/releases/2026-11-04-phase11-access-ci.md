# Phase 11 first batch: API access and basic CI

All business API routes now require the existing `EXPERIMENT_API_TOKEN`, including
legacy chat, project/document, RAG, Agent and resource endpoints. Authentication
runs before routing/body parsing; denied requests do not consume an upload stream
or dispatch a model/tool. Minimal liveness remains public; readiness requires the
token. Production API docs are disabled.

Host and Origin admission, explicit CORS origins, production credential checks,
bounded optional secret files and input-hidden configuration errors are added.
Secret files and direct values cannot conflict; empty optional `_FILE` entries in
the public example remain unconfigured. The default API image disables proxy
header trust, and Compose readiness supplies the configured access token.

Existing manual API examples now include explicit credentials. The browser keeps
its current in-memory token flow; local integration fixture management requests
also authenticate. See [the access and configuration guide](../security/access.md)
for compatibility, startup and rotation instructions.

Checks defines Linux backend, Windows file/auth compatibility and frontend jobs.
Dependencies are locked, Actions use verified immutable commits, job permissions
are read-only, checkout does not persist credentials, and CI has no supplier or
deployment secrets. Remote image publishing and deployment are not enabled.

Local verification covers 1331 distinct backend nodes: **1328 passed, 3 skipped**,
assembled from disjoint suite partitions and focused repaired/new-case reruns.
Ruff and full mypy (276 files) pass. Frontend typecheck/build, 97 unit tests,
21 browser-fixture cases and 14 real local HTTP/browser cases pass. Supplier
LLM/search calls are zero. The existing Compose smoke switch and two Windows
symlink-permission checks remain skipped; Linux CI exercises symlink behavior.

Initial route-introspection and anonymous-validation expectations were corrected
in tests, with their failure reports retained locally. The first browser run's
Windows teardown required ownership-verified cleanup of its test Vite process;
the real HTTP suites then ran with normal owned-process cleanup. A timed-out
initial fixture startup is retained; the final fixture probe uses public
liveness with a bounded cold-start allowance. No normal services, real secrets,
runtime records or design documents were changed or published.

The first hosted Checks run passed Windows, but exposed Linux-only mypy issues
in Git process cleanup and the evaluation worker's mixed asynchronous result
types, plus a browser test with a hardcoded Windows drive. Platform-aware
narrowing, explicit waiter/result types and a real test-file-relative path fix
these without weakening checks. Local Linux-target mypy and 60 focused Git/eval
tests pass, along with the corrected filesystem-denial browser case. The original
[failed hosted run](https://github.com/windskyther/AutoScholar/actions/runs/41769557139)
is retained separately from subsequent checks.

This batch is independently integrable API hardening and a CI foundation.
Phase 11 still requires request limits/audit, sandbox control-plane hardening,
production ingress/images, complete release gates, backup/restore and final
production-shape acceptance. It does not declare the entire phase accepted or
retroactively change any Phase 10 model outcome.
