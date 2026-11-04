# Phase 11D: sandbox control and bounded replan diagnostics

The Docker manager now requires a separate internal credential before parsing input
or creating containers. Callers, smoke commands and Compose pass this credential;
generated programs do not receive it. Manager execution has an action allowlist,
bounded input and concurrency, while Docker objects require ownership proof before
use or removal. Cleanup survives repeated cancellation. Strict artifact parsing and
bounded log collection preserve sandbox resource limits.

The workflow retains its two-attempt correction limit. New schema diagnostics identify
the failing field and numeric bound; repair constraints expose remaining code-repair
capacity and immutable step types. Synthetic wire-level fixtures reproduce all three
recorded Phase 10 replan failure sequences, including charged invalid completions.
They verify safe stop, constrained successful correction and no retry for truncated
or unmetered output. Cross-source adversarial fixtures cannot obtain a control/secret
tool or enlarge the server budget. Original evaluation outcomes are not rewritten.

Validation includes the existing workflow, API and security regression selection,
strict mypy/Ruff, and real Docker checks using cached pinned images, current read-only
source and an independent empty dataset volume. Real execution verifies unauthorized
requests create no containers; network/root/dataset isolation; ordinary artifact
collection; timeout; repeated-cancel cleanup; and symlink-artifact rejection. All
12 created execution resources are verified removed. No supplier API is called.

An initial real check exposed a local logging-driver option incompatibility: default
compression cannot retain only one log file. Explicitly disabling compression fixes
it while preserving the size limit. Initial failures are retained separately from
the successful rerun and ordinary-artifact check.

See [the sandbox operating guide](../security/sandbox.md) for the new credential and
trusted-controller boundary. No normal application was deployed or restarted by this
module. Phase 11E–11H remain: production images/ingress, release gates, backup/restore
and final acceptance.
