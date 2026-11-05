# Supply-chain checks

Checks runs a separate read-only `supply-chain` job on a temporary GitHub-hosted
Linux runner. It installs the locked `security` dependency group, checks tracked
source with Gitleaks, verifies a generated secret-scanner negative control, and audits
the frozen Python dependency graph and npm lockfile. No real service credentials,
local designs or runtime data are copied into the scanner input. Ordinary checks
have no package-publication or deployment permission.

```powershell
uv sync --frozen --group security
.venv\Scripts\python.exe scripts/install_security_tools.py gitleaks
.venv\Scripts\python.exe scripts/check_repository.py --gitleaks data/tooling/gitleaks/8.30.1/gitleaks.exe
.venv\Scripts\python.exe scripts/check_scanner.py --gitleaks data/tooling/gitleaks/8.30.1/gitleaks.exe
.venv\Scripts\python.exe scripts/audit_dependencies.py
```

Gitleaks and Trivy executable downloads use fixed versions and SHA-256 digests from
their official release metadata, recorded in `security/tools.json`. The installer
verifies cached downloads too and extracts only the expected executable. Python
audit tooling and its transitive dependencies are in `uv.lock`; scanners remain
outside the production dependency group.

The source scan rejects tracked `.env`, private design paths, runtime data, service
configuration directories and private-key files. Only the public root environment
examples are allowed. A single known synthetic offline token in an existing test
has a line-specific annotation; test directories are fully scanned. Reports redact
secret matches completely. The negative control constructs a public nonfunctional
credential at runtime and requires the scanner to reject it.

All known Python advisories block the dependency check; npm high/critical advisories
block, including transitive paths. Image-report evaluation retains unfixed
high/critical and unclassified findings. Missing inventory, unsupported formats and
failed scanner operations fail closed. The check queries current public advisory
services, so a previously green commit may need a new review as advisories change.
See [pip-audit](https://github.com/pypa/pip-audit) and
[Trivy vulnerability scanning](https://trivy.dev/docs/v0.76/guide/scanner/vulnerability/).

`security/exceptions.json` records the [2026-11-05 scoped runtime review](container-review-20261105.md).
Every exception must name an
individual package/advisory, a responsible owner, a concrete reason and an expiry
within 90 days. Image exceptions also require an exact image profile, installed
version and HTTPS source; other images/versions stay blocked. Wildcard or expired exceptions fail. Do not add bulk exclusions or
use `--ignore-unfixed` to turn unresolved vulnerabilities into a passing release.

Initial Phase 11F audits found seven affected Python packages and a Vite development
server advisory. The lockfiles now select fixed versions, with lower bounds preventing
an accidental downgrade. Existing workflow, document-processing and frontend tests
must still pass after these updates.

Available container fixes and the remaining upstream risks are documented in
[the container remediation report](container-vulnerabilities.md). The `containers`
job runs after the source/dependency, Linux, Windows and frontend jobs. It builds
sealed images and exercises isolated migration, browser/worker/CPU, HTTP/TLS,
normal/recovery workflows and backup/restore gates without supplier credentials.

The separate `Publish reviewed images` workflow is manual-only, from `main`, with
the `release` environment and package-write permission restricted to that job.
It rebuilds and rechecks the exact dispatched commit, requires successful Checks
for that same main revision, verifies each image revision and publishes explicit
version tags with recorded registry digests. Ordinary pushes and PRs cannot enter
that workflow. Configure a required reviewer on the GitHub `release` environment
if separate human approval is required. Publication and real deployment have not
been performed by these source changes; a local release plan also publishes nothing.
