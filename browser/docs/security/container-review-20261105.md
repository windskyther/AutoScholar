# Container advisory review, 2026-11-05

This is a scoped runtime assessment, not a claim that upstream vulnerabilities are
patched. Maintainer role: AutoScholar security maintenance. Review expires
2026-11-19. The exact package, installed version and image profile are recorded in
`security/exceptions.json`; a new version, advisory, profile or expiry fails the gate.
The original raw scanner output remains intact. These decisions cover the local,
single-operator production Compose and isolated CPU sandbox only. Public multi-user
hosting, additional mounts, elevated sandbox privileges and arbitrary use of these
images require a new review.

## Evidence and assumptions

- Production data uses PostgreSQL. The API accepts PDF uploads and bounded source/
  artifact operations; it exposes no SQL console, SQLite database import or
  SQLite changeset operation. Restricted Python tools do not permit SQLite imports.
- Generated source runs in a dedicated UID 65532 container with no network, tokens,
  Docker socket, SUID/SGID executables or added capabilities; root/dataset mounts
  are read-only and CPU, memory, PID, output and execution time are bounded.
  Source/artifacts are isolated per task, not shared process memory.
- API/workers run as UID 10001 with read-only roots and dropped capabilities. The
  separately authenticated Docker manager is trusted control-plane code. It does
  not run user-supplied commands in its own process or use mount/nsenter/ACL tools.
- Containers have no interactive login, configured fstab mounts or systemd-homed
  service. The generated-code process cannot acquire privilege from host tools.
- `tests/integration/phase11_cpu_image_acceptance.py`, sandbox security regressions,
  the rendered production configuration checks and isolated browser/worker stack
  enforce these assumptions. A runtime failure invalidates release acceptance even
  when the advisory exception matches.

## Individual decisions

Each row is assessed separately. Binary-package aliases from the same Debian source
are enumerated individually in the exception file; they are not wildcard matches.

| Advisory | Trigger and runtime disposition | Primary source |
|---|---|---|
| CVE-2026-76642 | Privileged mount post-hooks. No fstab delegation, SUID or mount capability in generated-code containers; trusted services never dispatch mount hooks. Trigger excluded in reviewed configuration. | [Debian](https://security-tracker.debian.org/tracker/CVE-2026-76642) |
| CVE-2026-78408 | Root nsenter cgroup descriptor inheritance. No nsenter control-plane execution and no inherited cgroup descriptor supplied to generated code. Trigger excluded. | [Debian](https://security-tracker.debian.org/tracker/CVE-2026-78408) |
| CVE-2026-78409 | Authorized mount subtree traversal. No fstab delegation or privileged mount execution by a task. Trigger excluded independently of host kernel version. | [Debian](https://security-tracker.debian.org/tracker/CVE-2026-78409) |
| CVE-2026-78410 | Restricted bind-mount path replacement. User code has no privileged fstab helper; manager uses Docker API mounts after server-side validation. Trigger excluded. | [Debian](https://security-tracker.debian.org/tracker/CVE-2026-78410) |
| CVE-2026-54369 | Privileged pathname ACL operations. Services provide no ACL API; sandbox callers have no privilege or access to other task volumes. Trigger excluded. | [Debian](https://security-tracker.debian.org/tracker/CVE-2026-54369) |
| CVE-2025-69720 | infocmp CLI parser. Service paths do not dispatch it. A sandbox program could crash its own unprivileged, resource-bounded process; that residual task-local failure is accepted until expiry. | [Debian](https://security-tracker.debian.org/tracker/CVE-2025-69720) |
| CVE-2026-16742 | Logged-in homed-managed account. No homed service/account/session in the images; source-package library aliases do not establish the affected service. Trigger excluded. | [Debian](https://security-tracker.debian.org/tracker/CVE-2026-16742) |
| CVE-2026-9538 | Perl Archive::Tar allocation from archive headers. Services use bounded Python archive handling. A task choosing its own parser remains subject to sandbox memory/time limits. Residual task-local exhaustion accepted until expiry. | [Debian](https://security-tracker.debian.org/tracker/CVE-2026-9538) |
| CVE-2026-119371 | Recursive CTE expansion. API paths accept neither SQL nor untrusted SQLite databases. A sandbox task can already stop its own process; bounded task-local crashes are accepted, not host/process escape. | [Debian](https://security-tracker.debian.org/tracker/CVE-2026-119371) |
| CVE-2026-119372 | FTS3 snippet position-list lifetime. No service full-text SQLite query interface. In the sandbox, memory belongs to that task and contains no provider keys; bounded task-local memory corruption is accepted until expiry. | [SQLite fix](https://github.com/sqlite/sqlite/commit/a87bd2471a9c0606e78d3c3678bcd9cecaa0e731) |
| CVE-2026-119373 | FTS5 rowid-list merge. No service SQLite full-text data or query import. A sandbox process has no additional authority to gain through its own heap; bounded task failure is accepted until expiry. | [Debian](https://security-tracker.debian.org/tracker/CVE-2026-119373) |
| CVE-2026-119374 | Corrupted FTS5 secure-delete index. Uploaded SQLite files are not opened by service code. Task-private input and process memory remain inside the sandbox; bounded task failure accepted until expiry. | [SQLite fix](https://github.com/sqlite/sqlite/commit/d9de6eedbe918c074c7556400fa21df5960704d9) |
| CVE-2026-119375 | SQLite/WAL page-size mismatch. No service database-file import. Sandbox page-cache disclosure is within the task's own process, without secrets/other task mounts; that residual risk is accepted until expiry. | [SQLite fix](https://github.com/sqlite/sqlite/commit/e5bf119821f25be1d7f10ef0a2edbe175289166e) |
| CVE-2026-119376 | COLLATE expression recursion. No SQL execution interface in services. Sandbox task-local stack exhaustion is bounded by process/time limits and recorded as a failure, never promoted to success. | [SQLite fix](https://github.com/sqlite/sqlite/commit/d54522a5b832422ab893a1e9fa7a99ab314ea4e2) |
| CVE-2026-119379 | Malformed session changeset. Services expose no SQLite session API. Generated code can affect only its own unprivileged process and dedicated files; residual task-local corruption accepted until expiry. | [SQLite fix](https://github.com/sqlite/sqlite/commit/64459c87cc702d600704fb77ee40ca1782398a52) |
| CVE-2026-76844 | Native gzwrite continuation after write failure. Service compression uses Python streams, not native gz* file writers; backup tar is uncompressed. Task-private native-code failure remains sandbox bounded and is accepted until expiry. | [CNA record](https://github.com/CVEProject/cvelistV5/blob/main/cves/2026/76xxx/CVE-2026-76844.json) |

These are explicit short-lived risk dispositions. HIGH/UNKNOWN raw counts remain
visible in audit summaries. No `--ignore-unfixed`, missing-inventory allowance,
cross-profile waiver or scan-error exemption is enabled. Re-review is required
before expiry or immediately when any assumption changes. Stable signed distro
patches take precedence when available; replacing a package requires a fresh scan.
