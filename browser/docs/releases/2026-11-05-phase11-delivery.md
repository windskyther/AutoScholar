# Phase 11 production delivery

The delivery target is one trusted operator on a local CPU Docker host. Production
Compose serves prebuilt Web assets and authenticated API/SSE/downloads through
Nginx; internal services are not published. Credentials, local designs and runtime
data stay outside Git and the image build context. Docker Desktop remains on D:.

## Reproducible acceptance

From a clean commit, install the locked Python/frontend dependencies and Chromium:

```powershell
python scripts/build_production.py --sandbox --pull --refresh-system
python scripts/verify_stack.py --download-dataset
python scripts/verify_production.py
python scripts/verify_production.py --tls
python scripts/verify_production.py --workflow --download-dataset
python scripts/verify_production.py --workflow --fault-once --download-dataset
python scripts/verify_recovery.py
python scripts/audit_images.py
```

The `Checks` container job runs these gates after the full Linux backend, selected
Windows compatibility/security, frontend and dependency/secret checks. It uses a
temporary hosted runner, random projects, private fixture credentials and public
MNIST data. No supplier API is called. The explicit offline coordinator is test
code; it does not change production model decisions or historical evaluations.
HTTP/TLS acceptance checks authentication, Chinese report content, PDF indexing,
SSE replay/heartbeat, controls, idempotence, download hashes and upload rejection.
The CPU workflow checks one normal training run or a failed run followed by one
approved experiment retry, retaining both experiment records and budget accounting.

Local verification additionally exercises upgrade from the previously sealed
`dbb2cc4` API image to the candidate and rollback at the same migration head. Set
`verify_recovery.py --previous-api <retained-image>` to repeat that comparison.
On a fresh runner the default previous/current tag is identical: that part of CI
proves restore/resume compatibility, not a two-version upgrade.

Recovery validates new-volume PostgreSQL data, pending approvals, event/checkpoint
records, file hashes and vector payloads, retained Redis expirations, tamper
rejection and unknown-call refusal after a second backup/restore. It never starts
restored consumers without the explicit compatible-switch/resume gate. Owned
fixture resources are removed; original user volumes and evaluation outcomes stay.

## Release and operating limits

- The [runtime advisory review](../security/container-review-20261105.md) is temporary
  risk disposition, not upstream remediation. Fresh local scans show 52/0/52 raw
  blocking package/advisory pairs for API/Web/CPU and 0/0/0 unresolved after exact
  profile/version reviews. The reviews expire on **2026-11-19**; new, mismatched,
  missing-inventory, failed or expired results block the gate.
- [Maintenance and recovery](../operations/recovery.md) requires a stopped-writer
  maintenance window and new restore volumes. Database downgrades and uncertain
  invocation resends are refused. Backup encryption and off-device retention are
  explicit operator responsibilities.
- [Image publication](../security/supply-chain.md) is a separate manual main-only
  workflow, requiring successful Checks for the dispatched SHA and matching sealed
  image revisions. Ordinary pushes/merges do not publish or deploy. No registry
  publication or normal-service replacement was performed during implementation.
- Public hosting, a real domain/certificate chain, multiple users, GPU operation
  and a new paid model-quality evaluation are outside this delivery acceptance.

Final acceptance requires successful Checks on the exact integrated commit and
the corresponding local immutable-image/backup receipts under ignored
`data/validation/`. Preparing these commands alone does not declare acceptance.
