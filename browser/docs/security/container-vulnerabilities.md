# Container vulnerability remediation — 2026-11-05

This module fixes available application/runtime updates and retains raw upstream
findings. The initial counts below precede the individually scoped, expiring
[2026-11-05 runtime review](container-review-20261105.md). Reviewed findings remain
visible; new, mismatched or expired findings block publication. This document does
not itself accept Phase 11F or authorize publication. Normal services were not replaced.

## Implemented fixes

- API, build and CPU images use the official Python 3.12.16 Debian 13 slim image,
  pinned to its content digest. Both build and runtime layers apply signed Debian
  updates over HTTPS. uv 0.12.10 is separately digest-pinned and stays in the builder.
- The runtime copy contains the locked application environment, without the builder
  cache or uv. API and CPU images remove the unused pip installer after installation,
  clear all SUID/SGID executable bits and use a noninteractive terminal setting.
- Nginx runtime packages are updated through the signed Alpine repositories, fixing
  the detected Brotli, curl/libcurl and Expat advisories.
- The CPU image uses PyTorch 2.10.0+cpu / torchvision 0.25.0+cpu and fixed Pillow,
  filelock and packaging dependencies. `docker/sandbox/requirements.txt` pins the
  complete Linux x86_64 / Python 3.12 graph with hashes. The input file is maintained
  separately; the generated file is used with `pip --require-hashes`.
- Production defaults to `autoscholar-python-sandbox:phase11`. Existing phase4 images,
  normal data volumes and historical experiment results are preserved. New training
  measurements belong to the new runtime and do not rewrite previous measurements.

The Python base update fixes the previously detected critical SQLite issue, while
newer upstream SQLite findings remain. See the [official Python image](https://hub.docker.com/_/python)
and [Debian's CVE-2025-7458 status](https://security-tracker.debian.org/tracker/CVE-2025-7458).
The CPU pair follows the [official PyTorch CPU installation matrix](https://pytorch.org/get-started/previous-versions/).

## Historical remediation and current review

Counts below are unique **package/advisory pairs**, including HIGH, CRITICAL and
UNKNOWN severities. They are not counts of distinct CVEs; one source-package issue
can appear against multiple binary packages. Raw scanner reports are retained locally.

| Image | Preserved initial report | After remediation | Remaining CRITICAL |
|---|---:|---:|---:|
| API | 140 | 51 | 0 |
| Web | 7 | 0 | 0 |
| CPU sandbox | 125 | 51 | 0 |

The initial API report precedes the separate Python dependency fixes. Its raw 188
records include duplicate Python detections; the table consistently deduplicates
package/advisory pairs. The remaining 51 pairs in each Debian image consist of
45 HIGH and 6 UNKNOWN records across 15 upstream advisory IDs. Runtime Python-package
high/critical/unknown findings are zero in both images.

| Remaining group | Trigger assessment and disposition |
|---|---|
| util-linux: CVE-2026-76642, CVE-2026-78408/78409/78410 | Privileged mount hooks, namespace/cgroup operations or authorized fstab paths are involved. The application does not dispatch these utilities; sandboxes have no network or Docker socket, use only dedicated workspace/dataset mounts, drop capabilities and prohibit gaining privileges. SUID/SGID bits are removed. Each exact package/version pair is covered by the linked temporary review; raw findings stay visible. |
| libacl1: CVE-2026-54369 | Requires a privileged caller applying pathname-based ACL operations to attacker-controlled paths. Application file handling does not expose an ACL-management API. The linked review records a temporary scoped disposition, not an upstream patch. |
| ncurses: CVE-2025-69720 | Upstream identifies the `infocmp` command-line parser. Services run without a TTY and do not dispatch it. The binary-package findings remain recorded. |
| SQLite: CVE-2026-119371/119372/119373/119374/119375/119376/119379 | Crafted SQL, SQLite files or extensions are implicated. Production services use PostgreSQL and do not accept uploaded SQLite databases; generated code remains confined to its own sandbox. The linked review records each exact alias/version separately, including UNKNOWN records; new or expired findings still block. |
| systemd: CVE-2026-16742 | Upstream concerns a logged-in homed-managed account. These service containers do not run systemd-homed. The library/source-package records remain recorded. |
| Perl: CVE-2026-9538 | Upstream concerns Archive::Tar processing attacker-sized entries. Application archives use the bounded Python path, not Perl Archive::Tar. The package record remains recorded. |

These are assessments of the current service and sandbox configuration, not claims
that arbitrary use of the image is unaffected. The root Docker manager remains a
trusted control-plane exception. Changing entrypoints, capabilities, mounts or
installing additional dependencies requires a new assessment.

Primary upstream descriptions: [mount hooks](https://security-tracker.debian.org/tracker/CVE-2026-76642),
[nsenter](https://security-tracker.debian.org/tracker/CVE-2026-78408),
[mount subtree](https://security-tracker.debian.org/tracker/CVE-2026-78409),
[restricted mounts](https://security-tracker.debian.org/tracker/CVE-2026-78410),
[ACL](https://security-tracker.debian.org/tracker/CVE-2026-54369),
[infocmp](https://security-tracker.debian.org/tracker/CVE-2025-69720),
[SQLite FTS5](https://security-tracker.debian.org/tracker/CVE-2026-119373),
[systemd-homed](https://security-tracker.debian.org/tracker/CVE-2026-16742),
[Archive::Tar](https://security-tracker.debian.org/tracker/CVE-2026-9538).

The initial scan used an empty `security/exceptions.json`. The subsequent review
records each advisory and binary-package alias separately for its exact profile
and installed version, with expiry on 2026-11-19. There is no wildcard suppression or
`--ignore-unfixed`. Image exceptions must match the exact image
profile and installed package version and provide a primary HTTPS source; a review
of one image/version cannot waive another. Package updates or newly detected
advisories therefore require new review.

## Build and repeat the checks

From a clean commit, with the project virtual environment and Docker available:

```powershell
.venv\Scripts\python.exe scripts/build_production.py --sandbox
.venv\Scripts\python.exe scripts/install_security_tools.py trivy
.venv\Scripts\python.exe scripts/audit_images.py
.venv\Scripts\python.exe scripts/verify_production.py
.venv\Scripts\python.exe scripts/verify_production.py --tls
```

Use `--development` only for a deliberately unsealed working-tree build. The optional
`PYTHON_RUNTIME_IMAGE` build argument permits a verified mirror of the pinned base;
the standard content digest must be retained. The production Compose file resolves
both the sandbox tag and runtime base for the build helper. No command publishes an
image or updates running normal services.

The audit reads immutable local image IDs, saves complete package/vulnerability
inventories under `data/validation/`, and evaluates API, Web and CPU profiles
separately. It exits **1** for unresolved, mismatched or expired findings; scan errors also
fail. The refreshed 2026-11-05 inventory contains 52/0/52 raw blocking pairs for
API/Web/CPU, including the newly reported zlib CVE-2026-76844. All 104 reviewed
pairs match the scoped decisions through 2026-11-19; unresolved counts are 0/0/0.
This is conditional risk disposition, not a claim of zero raw vulnerabilities. A newly green source/dependency CI run is not permission to publish an image.

Compatibility checks cover actual PostgreSQL/Redis/Qdrant and HTTP/HTTPS browser
flows, plus a fresh Docker controller using only a verified public MNIST copy.
The CPU check exercises isolation, static checks, pytest, one 128-sample/one-epoch
MLP/CNN comparison and five verified artifacts. It is an engineering check, not
statistical model-quality evidence. Temporary execution resources are ownership
checked before cleanup; no supplier API is called.

Continue tracking patched stable Debian packages, rebuilding and rescanning when
available. Completion requires the runtime constraints, container CI and manual
publication gate to pass together; the existence of a review record is insufficient.
