# Sandbox control plane

The manager is a trusted Docker controller. Only this dedicated service receives the
Docker socket; API, browser, workflow workers, MCP processes and generated-code
containers do not receive it. Docker control can confer host control. An internal
network, a container boundary or a read-only socket mount does not remove that trust
requirement. Operate one manager process, without multiple Uvicorn workers or replicas.

Before starting the upgraded services, generate a separate random credential locally:

```powershell
uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Place it in the local `.env` as `SANDBOX_MANAGER_TOKEN`. API, Workflow Worker,
experiment MCP and manager must share this value. Keep it distinct from
`EXPERIMENT_API_TOKEN`, `MCP_SERVICE_TOKEN` and supplier keys. The application does
not generate or overwrite your local credentials automatically. This module does not
restart or deploy the normal installation.

Direct service configuration also accepts `SANDBOX_MANAGER_TOKEN_FILE`, a bounded
UTF-8 file with a final newline allowed. Configure a value or a file, not both. Empty
file settings select no file. Tokens require 32–4096 printable ASCII characters without
whitespace; unconfigured execution fails closed. File mounts must be read-only and
accessible to each calling service; a host path alone does not mount it into Compose.
Production secret mounts and ingress are completed separately in Phase 11E.

`GET/HEAD /health/live` is public and does not call Docker. All other manager routes,
including `/internal/health` and `/internal/v1/run`, require a single exact Bearer
credential. Wrong/missing credentials return 401 before reading the body or calling
Docker; an unconfigured service returns 503. Browser Origin headers are rejected.
Clients ignore environment proxies and do not follow redirects with the credential.
Health probes use public liveness; authenticated health retains image/dataset checks.

The manager admits two concurrent requests by default and returns 429 with
`Retry-After` when busy. `SANDBOX_MANAGER_MAX_CONCURRENT` and
`SANDBOX_MANAGER_MAX_BODY_BYTES` configure finite limits (default body cap 16 MiB).
Actual body bytes are counted, including chunked bodies; input has a 15-second idle
and 60-second total deadline. `SANDBOX_ALLOWED_ACTIONS` is a nonempty JSON list drawn
from `static_check`, `run_python`, `run_pytest`, `run_shell`. The shell route still
allows only Python, pytest and Ruff. Unknown privilege, mount, network and executable
fields cannot extend the validated request schema. No Docker settings come from
model-supplied fields.

Each execution retains network mode `none`, UID 65532, dropped capabilities,
no-new-privileges, CPU/memory/PID limits, a read-only root filesystem and dataset,
and a fresh source volume. Its environment contains no manager/API/supplier keys.
The loader is also isolated, with smaller resource limits. Docker logs rotate with
a finite local-driver size; HTTP log reads stop at a bounded prefix before decoding.
Artifacts have bounded input, member count, individual and total sizes. Compressed
tar, traversal, wrong file paths, links, special devices and sparse members are rejected.

Temporary objects carry instance and operation labels. The controller inspects labels
before use and deletion; a matching prefix alone does not authorize removal. Lost
create acknowledgements use a preallocated name plus the same label proof. Cancellation
joins bounded cleanup even when cancellation repeats. Engine failure or mismatched
ownership retains uncertain objects and logs `sandbox_cleanup_incomplete` with the
operation ID; inspect and verify ownership before operator cleanup. Never use a broad
Docker prune to recover one task.

Coordinator input from web excerpts, PDF/RAG, project memory, tool output and Reviewer
diagnoses remains untrusted data. Tests supply malicious text at each input position
and force attempted control/secret tool calls; the exposed tool schema and machine
checks reject them. This is a finite permission-boundary test, not a promise that a
model recognizes every possible injection. Existing filesystem, task budget, approval,
source-handoff and independent artifact checks remain authoritative.

The three Phase 10 replan failures have synthetic minimal offline reproductions:
trailing JSON data; a coding rerun with no repair budget followed by malformed JSON;
and malformed JSON followed by an oversized description. Correction now reports
schema field/type/numeric limits and immutable repair constraints without copying
arbitrary output. A valid model correction may proceed, within two total attempts.
The server never writes a substitute plan, adds attempts, changes approval or awards
PASS. Truncated output stops; unknown usage retains pending Journal state and does
not become zero. Historical raw responses, scores and independent rejection results
are preserved unchanged.
