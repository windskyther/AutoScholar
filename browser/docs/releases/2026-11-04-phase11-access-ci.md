> Historical release note: the user access-token layer described below has since
> been removed for local-only operation. See [current access](../security/access.md).

# Phase 11 first batch: API access and basic CI

The original API access-token layer was removed for local-only operation.
Host/Origin boundaries, bounded requests and secret-file handling remain.
Language models are now registered exclusively in model management; their keys
are encrypted independently of HTTP access. See the current access guide.

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

The repaired source `efc01cc11745923ae62cfe2d9c62208c3a0ffde3` passed the
[hosted Checks run](https://github.com/windskyther/AutoScholar/actions/runs/41770677315):
Linux backend **1330 passed, 1 skipped**, Windows **69 passed**, and the frontend
job including all 21 browser cases. Linux executes the two symlink checks that
are skipped on this local Windows account; these counts describe the same suite,
not additional unique tests. Both platforms also pass mypy.

The main-based combination `7d59b56fefa3e95b7c29556b232412b8c216ac77` matches
the repaired dev tree and passed another **111 focused checks**, Ruff and full
mypy (276 files). This verified batch has been integrated into main. These checks
overlap existing coverage and are not added to the unique regression total.

This batch is independently integrable API hardening and a CI foundation.
Phase 11 still requires request limits/audit, sandbox control-plane hardening,
production ingress/images, complete release gates, backup/restore and final
production-shape acceptance. It does not declare the entire phase accepted or
retroactively change any Phase 10 model outcome.
