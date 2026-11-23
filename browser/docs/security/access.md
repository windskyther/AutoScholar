# Local API access and model encryption

AutoScholar runs as a local, single-user workbench. Business API routes do not
require a login, bearer token or browser-stored credential. The former experiment
access layer and its REST router have been removed. Health probes need no token.

Development Compose binds the API to 127.0.0.1. The workbench proxy accepts only
loopback peers with a matching local Host and same-origin browser requests. It
never reads .env and never injects an Authorization header. The API validates Host
and Origin before consuming a request body; forwarded headers do not override
these checks. CORS does not enable credentialed requests. Production API docs
remain disabled. Bind local services to loopback when running outside Compose.

Every language model must be registered in model management and selected for a
request or task. Environment model settings cannot supply a fallback model.
Registered API keys are encrypted in the database using MODEL_ENCRYPTION_KEY.
This key is never used for HTTP authentication or sent to the browser/provider.
Preserve it with database backups. The local upgrade retains the previous key
material under this new name so existing saved model keys remain readable.

Secret values or bounded UTF-8 _FILE settings are supported for model encryption,
embedding, search and infrastructure services. Do not put secrets, design files
or runtime data in Git. Internal sandbox and MCP services retain their distinct
service credentials; these do not implement user login.

Request limits, audit records, cancellation fencing, task ownership and artifact
integrity checks remain in effect. The supported entrypoint is the local
workbench. This configuration does not provide multi-user or public hosting.
