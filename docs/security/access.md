# API access and production configuration

Phase 11A/11B protect the existing single-operator API. This is an access contract
and operating guide for implemented behavior; the local design and planning files
remain outside Git.

| Entry point | Policy |
|---|---|
| `GET /health/live` | Public minimal liveness; no dependency, model or user-data queries |
| `/health/ready` | Bearer access; checks configured dependencies |
| `/chat`, every `/agent/*` mode and task resource | Bearer access before request-body parsing and dispatch |
| `/projects/*`, documents, RAG and Memory | Bearer access for reads and writes |
| `/workbench/*`, including events, controls, approvals and downloads | Same Bearer access; existing facade checks remain |
| Development `/docs`, `/redoc`, `/openapi.json` | Bearer access |
| Production API documentation | Disabled; authenticated requests receive 404 |
| Other paths or methods | Authentication precedes routing; then normal 404/405/422 behavior |
| Browser preflight | Only configured origins/methods/headers; carries no business data |

Configure `EXPERIMENT_API_TOKEN` and send `Authorization: Bearer <token>` for all
business requests. The existing token name is retained for compatibility; it now
protects all API capabilities. Missing configuration returns 503; absent, wrong,
malformed or repeated authorization headers return 401. A request ID and a stable
JSON error are returned without reading the request body or invoking tools.
There is no implicit anonymous development mode.

Production additionally requires a randomly generated access token of at least
32 characters, explicit `API_ALLOWED_HOSTS`, and a PostgreSQL password of at least
16 characters that passes the obvious-weak-value checks. Generate random values,
not memorable passwords or the public examples used by tests. Production remote
origins require HTTPS. The access token must differ from service/provider keys.
Validation errors hide their input values.

`API_ALLOWED_HOSTS` and `API_ALLOWED_ORIGINS` are JSON arrays. Hosts are exact
hostnames/IP addresses without a port; the development `*` default is forbidden
in production. Origins include the scheme and optional port, with no path,
credentials or wildcards. Same-origin requests are allowed; cross-origin requests
require an explicit origin. Origins are an additional browser boundary and do not
replace Bearer authentication. CORS never grants cookie credentials.

The boundary validates the actual Host and Origin headers. Forwarded headers do
not replace these values. Run Uvicorn with `--no-proxy-headers` until a separately
verified trusted-proxy deployment configuration is installed. The default image
uses this option. Production ingress and internal-service isolation are subsequent
Phase 11 modules, not provided by this API change alone.

Provider/access secrets may be supplied using either their existing environment
variable or a corresponding `_FILE` variable pointing to a secret file:
`EXPERIMENT_API_TOKEN`, `LLM_API_KEY`, `EMBEDDING_API_KEY`, `TAVILY_API_KEY`,
`SEMANTIC_SCHOLAR_API_KEY`, `QDRANT_API_KEY`, `MCP_SERVICE_TOKEN`.
Files must contain a nonempty UTF-8 value of at most 4096 bytes, with no internal
whitespace; a final line ending is accepted. A nonempty direct value together with
its file setting is rejected. Mount production secret files read-only outside
document/workspace directories. Do not place them in the image, browser bundle,
repository or generated-code sandbox. Compose file mounts do not themselves
encrypt secrets or restrict a privileged Docker administrator.

Secret files are read when Settings is constructed, not on every request. For
rotation, pause/drain existing operations, update the access credential and its
authorized clients, then restart every API instance together. Verify that the
old token receives 401 and the new token can read authenticated readiness before
resuming consumers. Update independent service/provider credentials only in the
services that need them; do not assume an edited file instantly revokes a running
process's cached credential. Existing production services require a separately
scheduled maintenance operation; the checks do not perform this rotation.

The API container's readiness probe authenticates using its configured token;
development without a token can still expose minimal liveness, but business access
and authenticated readiness remain unavailable. This release does not edit a
developer's `.env`, rotate real credentials, restart consumers or migrate data.

CI runs locked dependencies, backend tests and frontend checks with public test
credentials only. No supplier API keys, real data, designs, deployment credentials
or automatic model calls are part of Checks. Full production-stack testing now
runs in the isolated container job; image publication is a separate manual workflow,
and actual deployment remains an operator action. CI uses read-only
permissions, immutable Action commits and isolated hosted runners as described in
[GitHub's secure-use guidance](https://docs.github.com/en/actions/reference/security/secure-use).
