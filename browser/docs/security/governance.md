# Request governance and audit

All authenticated business routes use Redis admission and PostgreSQL security audit.
`GET /health/live` stays independent of these services. Rejected authentication never
reads the request body or dispatches business work. Forwarded headers and token rotation
cannot select a different quota bucket: the single-operator deployment shares stable,
category-specific Redis keys across API processes.

| Category | Requests / rolling minute | Active requests | Dispatch deadline |
| --- | ---: | ---: | ---: |
| read | 120 | 16 | 30 s |
| write/control/approval/recovery | 30 | 4 | 30 s |
| chat and RAG | 10 | 2 | 90 s |
| synchronous agent, including research/coding/training | 6 | 1 | 1800 s |
| PDF upload | 10 | 2 | 180 s |
| durable task submission | 20 | 4 | 30 s |
| SSE | 30 | 4 | 300 s |

`API_REQUEST_LIMITS` accepts a JSON object with all seven keys (`read`, `write`,
`llm`, `agent`, `upload`, `submit`, `sse`). Each value specifies positive `per_minute`,
`concurrent`, and `deadline_seconds`; JSON `null` selects the documented defaults.
Compose forwards these settings to the API and worker. Defaults are server enforced; payload budgets
cannot increase them. Agent's conservative shared cap also limits synchronous training.
Durable workers retain their existing task leases, approval gates, sandbox resource
limits, and original cumulative budget; an HTTP slot does not authorize a worker retry.

Redis Lua uses server time and atomically admits both rate and concurrency. Active
leases renew while a request runs; cancellation and completion release them, and a
crashed process's lease expires. A missed renewal cancels dispatch. Rate charges remain
until their rolling window ends, including failed requests. Redis failure returns
`503 admission_unavailable`; quota exhaustion returns `429 request_limit_exceeded`
with `Retry-After`. Neither dispatches business work. Use dedicated non-evicting Redis
capacity for governance; deleting/restoring its keys can change the effective quota.

Input is counted while reading, including chunked uploads and dishonest Content-Length.
JSON/general bodies default to 1 MiB. Multipart requests allow `DOCUMENT_MAX_BYTES`
plus 1 MiB overhead; PDF storage still independently enforces the exact file limit.
The spool keeps at most 1 MiB in memory and closes on every exit. Default body idle and
total deadlines are 15 s and 120 s. Oversize returns 413; slow input returns 408.
Business dispatch starts only after the complete bounded input is available.

Mutation responses buffer up to `API_RESPONSE_MAX_BYTES` (8 MiB) until completion
audit commits. Downloads stream within the larger configured document/artifact file
cap. Individual sends have a 15 s deadline. SSE retains the existing 10 s heartbeat
and 60 s rotation, plus a 45 s output-idle watchdog and its separate connection quota.
After streaming headers are sent, errors close the stream; clients reconnect using
the existing event cursor. A stream never sends a second HTTP status.

| Operation | Automatic HTTP retry policy |
| --- | --- |
| LLM / Tavily / Semantic Scholar / MCP / training | Zero supplier retries by default; unknown results stay unresolved |
| Redis admission, audit commit | Zero automatic retries; fail closed before dispatch |
| Explicit test/adapter search retry override | At most two delays, each 0–10 s; the original search deadline includes backoff |
| Durable submit / control / approval / recovery | Existing idempotency, sequence and approval rules; inspect existing state |
| PDF parsing and indexing jobs | Existing bounded job attempts, leases and document identity |
| Browser read/SSE reconnection | Existing bounded client policy; no replay of writes |

An HTTP timeout does not prove a model, search or sandbox operation was absent.
`external_operation` retains its pending Journal entry; no new invocation ID, budget
reset, restart or HTTP retry may silently turn that uncertainty into success or zero
usage. Synchronous routes retain their existing contracts; durable submission is the
recommended interface for long work and returns the existing task on an idempotent repeat.

Migration `20261104_0014` adds append-only `security_audit`. Apply
`uv run alembic upgrade head` before starting the upgraded application; this change does not migrate a running
installation automatically. Existing outcomes and Journal entries are not rewritten.

Audit stores allowlisted request ID, operator fingerprint, category/action, operation
correlation, UUID task/project IDs, phase, stable outcome/status and known token usage.
Idempotency keys are hashed; unknown usage remains SQL NULL. Authentication/boundary
denial audit is best effort. Authenticated requests commit admission audit before work;
critical writes cannot silently proceed when this persistence fails. The completion
record commits before a successful mutation response. If it fails after work,
`503 audit_outcome_unknown` means inspect the original operation; do not resend a
non-idempotent write. The committed admission record remains evidence of possible work.
Application startup commits a configuration audit event with a policy fingerprint and
fails if audit storage is unavailable.
Request-context external calls and worker Journal dispatches add tool audit records;
the original Journal is authoritative for pending results and cumulative budgets.

Default audit/logs exclude request bodies, prompts, documents, raw credentials and
URLs. Structured sensitive fields, configured secrets, Bearer values, URLs, foreign
HTTP-client logs and exception tracebacks are redacted centrally. Request logs use
route templates; raw Uvicorn access logging is disabled. Audit data remains private
operational data and is not committed to Git. Retention, backup and production proxy
configuration are addressed in later Phase 11 modules.

Offline functional fixtures explicitly inject in-memory stores implementing the same
admission contract; the application has no test-mode security bypass. Dedicated tests
exercise real Redis across separate Python processes and the complete PostgreSQL
migration chain using disposable services. No supplier credentials are required.
