# Repository working agreement

- Develop on `dev`; commit and push completed, verified modules to `origin/dev`.
- Owner instruction (2026-10-26): independently verified, cohesive changes may be
  integrated and pushed to `main` as they become ready, without waiting for an entire
  Phase or requesting separate approval for each such integration. This supersedes
  the earlier per-merge approval rule. Return the working checkout to `dev` afterward.
- For partial integration, include required dependencies and regression tests, and
  validate the resulting main-based tree. Keep incomplete or unverified work on `dev`.
  Record scope and evidence; a partial integration does not declare the Phase accepted.
- Never commit `.env`, local design documents, or runtime data under `data/`.
  Public `.env.example` is allowed; never include real credentials in it.
- Partial integration does not authorize new paid API calls, deployments, or changing
  historical evaluation outcomes. Retain the applicable separate authorization limits.
- Docker Desktop is installed on D:. Preserve that location and launch it hidden.
