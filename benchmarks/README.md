# AutoScholar-Eval benchmark contracts

Versioned suites separate `inputs` from `expected` gold labels. Execution adapters receive only
the prompt and inputs. All included Phase 10A/10B/10C/10D/10E cases are self-authored public engineering
fixtures; no provider requests, private design documents, credentials or runtime records are
included. The CLI implements RAG replay, controlled Research/Tool component execution and
explicitly injected real Docker Coding/Experiment and durable E2E execution. Controlled workflow
ablations and cached-neural reranker comparisons are implemented; real-LLM workflow ablations
remain unverified.

## Public RAG scoring suite

`rag/public_v1.json` contains 20 labeled queries over the original text in
`rag/ranking_fixture_v1.json`. The latter binds exact prompts to fixed output rankings for four
mode labels. Those rankings are artificial provider-response fixtures, **not Dense/BM25/Hybrid/
Reranker algorithm outputs**. They exercise scoring, duplicate handling, query bindings and
reproduction. Artificial rank positions are balanced; do not infer improvements between modes.

```powershell
Set-Location D:\98281\deepscholar
.venv\Scripts\python.exe -m autoscholar.evaluation validate --dataset benchmarks/rag/public_v1.json
if ($LASTEXITCODE -ne 0) { throw 'Dataset validation failed' }
.venv\Scripts\python.exe -m autoscholar.evaluation run
if ($LASTEXITCODE -ne 0) { throw 'Evaluation failed' }
```

Optional arguments: `--modes dense sparse hybrid hybrid_rerank`, `--ks 1 5 10`, `--repeats 2`,
`--seed 42`, `--timeout 30`, and `--output-root data/evaluation/custom`. Duplicate modes, more
than 10 distinct cutoffs, nonpositive cutoffs, cutoffs above 50 and invalid query/gold bindings
are rejected before starting a run. The default CLI profile is `offline` and does not
load `.env`, provision models, connect to the application database, or accept dynamic plugins.
Research uses a fresh in-memory SQLite database for each case, with development `aiosqlite`.

Results go into a fresh ignored `data/evaluation/…/eval-<UUID>` directory. Four modes over 20
cases produce 80 records at the default repeat count. Exit codes: 0 checks passed, 1 one or
more failed checks/runtime outcomes, 2 invalid configuration or IO, 130 user cancellation.

## Metric definitions

Document- and chunk-level labels are scored separately. K counts **returned chunk positions**:
documents are projected onto those positions, without compressing ranks. Repeated chunk or
document IDs consume positions but earn relevance gain only at the first occurrence. Recall
uses unique relevant IDs; NDCG uses binary gains and cannot exceed 1 due to duplicates. MRR is
truncated at the largest requested K; output beyond that cutoff is ignored. A level without
gold labels is not scored (it is not assigned zero).

The compatibility fields in the older JSONL evaluator remain chunk-first per case, falling
back to document labels. New `document_metrics` and `chunk_metrics` report separate means and
the number of labeled cases. Do not compare a mixed legacy average as if it were one level.

The unified replay checks whether each labeled level hits at the largest requested K. A check
failure is reported even though the metric calculation completed. Per-case scores and all
outcomes are kept. Metric averages always state scored/planned coverage. No E2E task-success
rate or monetary cost is inferred from a successful RAG replay.

## Research and Tool component suites

```powershell
.venv\Scripts\python.exe -m autoscholar.evaluation run --category research --repeats 2
if ($LASTEXITCODE -ne 0) { throw 'Research checks failed' }
.venv\Scripts\python.exe -m autoscholar.evaluation run --category tool --repeats 2
if ($LASTEXITCODE -ne 0) { throw 'Tool checks failed' }
```

`--category` chooses the bundled dataset/fixture defaults. An explicit `--dataset` can infer
the category; a conflicting explicit category is rejected. Research/Tool only use `baseline`;
RAG modes and `--ks` are rejected rather than silently ignored. Input prompts must match the
fixture exactly. Source snapshot mismatches and unsafe benchmark input bounds fail preflight.

Research executes the real Planner, query planner, search orchestration, evidence selection,
citation validation and repository, using scripted provider responses. URLs on `example.org`
are fictional identifiers, never fetched. Six successful reports exercise language prompts,
evidence ordering, duplicate search results, duplicate citations and Markdown. Four cases
expect refusal on missing providers, provider errors, empty evidence and nonexistent E99.
Provider failures, evidence unavailability and citation protocol errors have separate metrics.

Research scoring is independent of Agent status. Citation validity checks inline IDs against
persisted evidence and claim mappings. Citation correctness is the fraction of unique
claim/evidence pairs supported by the explicit gold claim/source annotation, with exact
source-snapshot SHA-256 binding and whitespace-normalized exact claim presence in the answer,
with the matching inline marker immediately following that claim. Swapping inline markers
cannot be masked by a correct structured mapping elsewhere in the report.
Claim support uses all required annotated claims as its denominator, including missing ones.
Duplicate references add no credit. Unannotated or changed sources get zero relevance/quality;
source means use unique selected sources. Empty selections have unknown relevance/quality.
Quality values (1.0 and 0.8) are illustrative manual fixture annotations exercising aggregation
and thresholds, **not measured website authority**. Exact annotated claims do not evaluate
arbitrary paraphrases, negation, uncited extra prose or general entailment. Independent
adversarial tests prove structurally valid wrong-source/unsupported reports are scored failed.

Tool fixtures predefine the tool name and arguments, then run the real local Calculator after
the existing Gateway schema validator. Twelve numeric cases and eight expected refusals cover
arithmetic, powers, functions, forbidden imports/attributes, bad syntax, zero division, bad
argument shapes and unavailable tools. Selection and arguments are scored separately; numeric
accuracy requires both plus the expected finite result within an absolute tolerance. Unknown
tools are not resolved dynamically. No shell, Python execution tool or external tools run.
The harness bounds expressions to 1000 characters/100 AST nodes and requires literal exponent
magnitude <=1000 for both `**` and `^`, before the synchronous Calculator executes. This limits
benchmark inputs and does not certify the production Calculator against arbitrary expressions.

All 30 contract cases can pass while Research completion and Tool execution success each
remain 0.6: expected refusals are not completed tasks. Tool result accuracy excludes cases
without an expected numeric answer (coverage 12/20). Research claim support includes missing
claims in expected failures; citation correctness without any citation is unknown, not perfect.
Do not interpret scripted selection accuracy as LLM ability. `model_calls` counts actual script
callbacks, vendor tokens and external calls are zero; monetary cost remains unknown. Raw
answers, tool output, exception messages and source bodies are not exported to run reports.
Runtime artifacts remain ignored under `data/evaluation`.

## Coding / Experiment isolated component suites

These categories require `--profile injected --sandbox-container <dedicated-controller>`.
They have **no host execution fallback**, remote Docker/TCP mode or dynamic plugin resolution.
Docker context metadata must select a local Unix socket/named pipe; subsequent commands pin
that context, so `DOCKER_HOST` cannot redirect public sources to a remote daemon. Controller
Compose ownership and its sole internal network are verified. Worker requests/responses are
bounded; EOF on cancellation cancels and joins the executor's cleanup. Local worker stderr,
source bodies and raw error messages are not exported in reports.

The controller image only needs the cached app dependencies, not Git or SQLite. The Windows
host runs the evaluator with development dependencies and isolated in-memory SQLite per case,
so Git revision/dirty state are measured locally. Only public source snapshots enter child
containers. The controller has a read-only source mount and no host port; child containers
have no network or Docker socket, non-root UID, read-only root/dataset, dropped capabilities,
no-new-privileges, 2 GiB / 2 CPU / 128 PID bounds. Temporary resources carry an exact evaluation
owner label; no global Docker prune or application-stack teardown is used.

Prerequisites: Docker Desktop running, cached `autoscholar-api:phase9-acceptance` and
`autoscholar-python-sandbox:phase4`, cached public `autoscholar_mnist_data`, and local development
dependencies. These instructions do not build/pull images, download data or use `.env`. Missing
resources fail closed. The public `.defaults` file contains image names only, not credentials.

```powershell
Set-Location D:\98281\deepscholar
$evalProject = 'autoscholar-eval-10d-' + [guid]::NewGuid().ToString('N').Substring(0, 10)
$controller = "$evalProject-sandbox-manager-1"
$priorEvalManager = $env:AUTOSCHOLAR_EVAL_MANAGER_IMAGE
$priorEvalSandbox = $env:AUTOSCHOLAR_EVAL_SANDBOX_IMAGE
try {
  $env:AUTOSCHOLAR_EVAL_MANAGER_IMAGE = docker image inspect autoscholar-api:phase9-acceptance --format '{{.Id}}'
  if ($LASTEXITCODE -ne 0) { throw 'Cached controller image missing' }
  $env:AUTOSCHOLAR_EVAL_SANDBOX_IMAGE = docker image inspect autoscholar-python-sandbox:phase4 --format '{{.Id}}'
  if ($LASTEXITCODE -ne 0) { throw 'Cached sandbox image missing' }
  docker volume inspect autoscholar_mnist_data --format '{{.Name}}'
  if ($LASTEXITCODE -ne 0) { throw 'Cached public MNIST missing' }
  docker compose --env-file benchmarks/phase10d.defaults -f benchmarks/phase10d.compose.yaml -p $evalProject up -d --pull never --no-build
  if ($LASTEXITCODE -ne 0) { throw 'Dedicated controller startup failed' }
  .venv\Scripts\python.exe -m autoscholar.evaluation run --category coding --profile injected --sandbox-container $controller --timeout 120
  if ($LASTEXITCODE -ne 0) { throw 'Coding engineering checks failed' }
  .venv\Scripts\python.exe -m autoscholar.evaluation run --category experiment --profile injected --sandbox-container $controller --timeout 180
  if ($LASTEXITCODE -ne 0) { throw 'Experiment engineering checks failed' }
  .venv\Scripts\python.exe tests/integration/phase10d_acceptance.py --sandbox-container $controller
  if ($LASTEXITCODE -ne 0) { throw 'Isolation/timeout/cancellation checks failed' }
} finally {
  # Only the unique project created above. No -v, global prune or normal autoscholar project.
  docker compose --env-file benchmarks/phase10d.defaults -f benchmarks/phase10d.compose.yaml -p $evalProject down --remove-orphans
  $env:AUTOSCHOLAR_EVAL_MANAGER_IMAGE = $priorEvalManager
  $env:AUTOSCHOLAR_EVAL_SANDBOX_IMAGE = $priorEvalSandbox
}
```

Use `--repeats 2` for repeat records (additional CPU time, no paid API calls). Runner seeds
are recorded; real experiment data/training seeds are fixed explicitly in each input fixture
(42 and 7), not silently changed by repeat number. Repetition is not independent scientific
evidence. Artifact/checkpoint files remain under ignored `data/evaluation/component-workspaces`.
Only the temporary controller/network are removed; cached images/data and local reports remain.

Coding has eight accepted implementations (including two successful repairs), one exhausted
repair and one independent-oracle rejection. The trusted generic checker and separate public
vectors are never supplied to the scripted author; only `solution.py` is transferred to the
new oracle sandbox, not author-provided tests or pytest configuration. Numerical outputs use
an absolute tolerance, bools do not count as numbers, and expected exceptions are explicit.
The checker is a bounded regression oracle for public code, not a tamper-proof grading boundary
against arbitrary hostile Python. `compile_success` covers the real compileall + Ruff stage.

Experiment runs all five real CPU trainings through `ExperimentService` with bounded settings
(<=512 training/test samples and <=2 epochs); the bundled runs use 128/128 and one epoch.
Two accepted seeds, two service metric/metadata refusals and one oracle-detected fabricated
accuracy exercise different failure layers. Every declared artifact is re-read from its
saved path and rehashed; configuration, source digest, metrics, report and repository must
agree. Checkpoints are reconstructed only in a separate restricted sandbox using
`torch.load(..., weights_only=True)`; real accuracy is recomputed against the matching test
indices, parameters/tensors checked, the full cached dataset digest recomputed and PNGs fully
decoded. No checkpoint is deserialized on the host. This verifies the fixed trusted pipeline,
not arbitrary model architectures, scientific significance or a full MNIST accuracy target.

Contract-pass rate can be 1.0 while Coding acceptance is 0.8 and Experiment acceptance 0.4:
expected failure detection is not successful task completion. Missing oracle results stay
unknown, not perfect; metric coverage and repair-success denominators are explicit. No E2E
task-success rate is scored here. Script callback counts are not vendor requests. Real neural
forward passes are not LLM calls; vendor tokens/external API calls remain zero and price is
not assumed. Source, oracle, template, image and dataset digests identify the measured resources.

## Durable end-to-end workflow suite

`end_to_end/public_v1.json` has five public engineering cases. The separate workflow fixture
contains only execution decisions and public sources; gold labels are never supplied to the
coordinator or author. The adapter runs the actual durable Planner -> Research -> Coding ->
Experiment -> Reviewer/Replanner -> Writer services. SQLite is fresh and file-backed per case
under `data/evaluation/workflow-workspaces/evale-<UUID>`; no application DB is used. The coding
node receives its real full-source snapshot (no unavailable read/list tool calls), and its
mandatory compileall/Ruff/pytest requests are bound to the exact source digest. Real code and
tensor execution occur only in the dedicated Docker children, never on the Windows host.

| Case | Workflow result | Independent task completion | Key check |
|---|---|---|---|
| normal | succeeded | true | Saved metrics, source, plots, checkpoints and report agree |
| restart | succeeded | true | Reopen committed DB and rebuild services after coding; preserve source and usage |
| recover | succeeded | true | Rules override scripted PASS; rerun experiment only; report latest reviewed output |
| budget | budget_exceeded | false | One callback budget; no training or final report |
| deception | succeeded | false | Schema-valid fabricated accuracy disagrees with actual checkpoint inference |

Restart is database/connection/service reconstruction BETWEEN committed units, with a new
worker owner, not full-process crash or cluster recovery. The invalid-metrics and fabricated-
accuracy faults are fixed transport mutations AFTER actual training. Original code is not
silently replaced. The checker re-reads ten saved artifacts and uses the separate trusted
checkpoint oracle; missing oracle measurements stay unknown and cannot count as detected
deception. Parent-child/history links, evidence/citations, source handoff and final/latest-only
report bindings must also pass. This closed-world source-snapshot check is not general semantic
entailment. The benchmark exposes the distinction between a coherent reported workflow and
actual measured correctness; it does not retrofit a general tamper-proof production Reviewer.

Expected check-pass rate is 1.0, workflow completion 4/5 and independently verified task
completion 3/5. Only the E2E adapter populates `task_success`; refusal/negative-case detection
never earns task-completion credit. Default 128/128 samples and one epoch are engineering
subsets, not scientific accuracy targets. Repeats retain the explicitly fixed input seeds
(42 and 7), not independent randomized scientific trials.

Persistent usage is checked against actual callback/search/sandbox/training counts. Script
responses intentionally declare synthetic `TokenUsage(2, 1, 3)` to exercise accounting: only
`budget_tokens` reports these units. No vendor tokenized/generated the response, so measured
vendor tokens and external API requests remain zero; script callbacks are not paid visits.
Price remains unknown. Independent grading sandbox runs are separately counted and excluded
from the task's execution budget. Interrupted cases without a returned observation retain
unknown measured usage rather than inventing zero.

Use the same cached images/data as 10D. This complete PowerShell block pins a local Docker
context before starting a unique controller, never uses `.env`, never downloads/builds images,
and removes only that temporary project. Docker Desktop must already be running.

```powershell
Set-Location D:\98281\deepscholar
$evalProject = 'autoscholar-eval-10e-' + [guid]::NewGuid().ToString('N').Substring(0, 10)
$controller = "$evalProject-sandbox-manager-1"
$evalContext = (docker context show).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Docker context unavailable' }
$evalEndpoint = docker context inspect $evalContext --format '{{json .Endpoints.docker.Host}}' | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $evalEndpoint -notmatch '^(npipe|unix)://') { throw 'Local Docker context required' }
$priorEvalManager = $env:AUTOSCHOLAR_EVAL_MANAGER_IMAGE
$priorEvalSandbox = $env:AUTOSCHOLAR_EVAL_SANDBOX_IMAGE
try {
  $env:AUTOSCHOLAR_EVAL_MANAGER_IMAGE = docker --context $evalContext image inspect autoscholar-api:phase9-acceptance --format '{{.Id}}'
  if ($LASTEXITCODE -ne 0) { throw 'Cached controller image missing' }
  $env:AUTOSCHOLAR_EVAL_SANDBOX_IMAGE = docker --context $evalContext image inspect autoscholar-python-sandbox:phase4 --format '{{.Id}}'
  if ($LASTEXITCODE -ne 0) { throw 'Cached sandbox image missing' }
  docker --context $evalContext volume inspect autoscholar_mnist_data --format '{{.Name}}'
  if ($LASTEXITCODE -ne 0) { throw 'Cached public MNIST missing' }
  docker --context $evalContext compose --env-file benchmarks/phase10d.defaults -f benchmarks/phase10d.compose.yaml -p $evalProject up -d --pull never --no-build
  if ($LASTEXITCODE -ne 0) { throw 'Dedicated controller startup failed' }
  .venv\Scripts\python.exe -m autoscholar.evaluation run --category end_to_end --profile injected --sandbox-container $controller --timeout 360 --repeats 2
  if ($LASTEXITCODE -ne 0) { throw 'E2E engineering checks failed' }
  .venv\Scripts\python.exe tests/integration/phase10e_acceptance.py --sandbox-container $controller
  if ($LASTEXITCODE -ne 0) { throw 'Real E2E cancellation/cleanup checks failed' }
  .venv\Scripts\python.exe tests/integration/phase10d_acceptance.py --sandbox-container $controller
  if ($LASTEXITCODE -ne 0) { throw 'Shared isolation/timeout/cancellation checks failed' }
} finally {
  docker --context $evalContext compose --env-file benchmarks/phase10d.defaults -f benchmarks/phase10d.compose.yaml -p $evalProject down --remove-orphans
  $env:AUTOSCHOLAR_EVAL_MANAGER_IMAGE = $priorEvalManager
  $env:AUTOSCHOLAR_EVAL_SANDBOX_IMAGE = $priorEvalSandbox
}
```

The E2E cancellation script waits for a REAL running training child before cancelling. It
verifies joined cleanup, zero owned child containers/volumes, a re-openable local DB, durable
`recovery_required` with unresolved operation accounting, and no automatic training replay.
The normal service/DB stack is never recreated or migrated; cached images/public data and
local reports/SQLite/workspaces are retained. This does not exercise HTTP/browser deployment,
real provider reasoning, real semantic RAG, or all possible hostile-code/checkpoint attacks.

## Controlled execution ablations

`ablations/workflow_v1.json` contains three public cases and independent baseline/override
labels; `workflow_fixture_v1.json` contains execution inputs only. Five actual variants run
the same normal, invalid-metrics recovery and fabricated-accuracy inputs. The baseline and
all variants seed the same real public project Memory and pre-chunked public project document.
Project-scoped Research requires a document source, so the actual local Qdrant/RAG chain supplies
a fourth bound evidence/citation in addition to three fixed web sources. No fake knowledge
retriever, verified historical experiences or private design text is supplied.

| Variant | Actual intervention | Retained checks |
|---|---|---|
| baseline | Stock model Planner/Reviewer/Replanner plus actual Memory | All execution and independent grading |
| no_planner | Validated static DAG instead of root model planning | Child planning, DAG validation, budget, audit |
| no_reviewer | Rules-only review instead of the model Reviewer | Rules, saved review, Writer recheck, checkpoint oracle |
| no_memory | Bypass Memory reads and learning | Same seeded project/documents and current specification |
| no_replanning | Explicit failure before a revision/model/training retry | Failed attempts retained; no failed final report |

Expected checks pass for all 15 records. Baseline/no_planner/no_reviewer/no_memory complete
2/3 tasks; no_replanning completes 1/3. Fabricated accuracy never earns task success.
No Reviewer still recovers because deterministic rules detect invalid metrics. This is
deliberate safety retention, not a full removal of all review/integrity mechanisms. Fixed
decisions do not measure real planning/review intelligence or Memory's reasoning benefit.
Seeded Memory contains only current public project constraints. Successful recovery may
record an actual provenance-backed experience through the stock service; No Memory bypasses
that learning. No fake previous experience is created to improve results.

`ablations/retrieval_v1.json` has five independent English relevance labels and a separate
eight-chunk public text fixture. Dense vectors and sparse counts are computed from text by
bounded bag-of-words algorithms; real Qdrant Local cosine/sparse/RRF searches execute, followed
by real Jaccard token-overlap sorting or no reranking. There are no canned rankings, model
downloads or vendor requests. Candidate limit and final retrieval K are identical in both
variants, and a baseline-first audit compares each actual ordered candidate pool. Stable ties
retain RRF order. UUID indexing IDs map back to public relevance IDs for document/chunk scoring.
Relevance outcomes are measured metrics, not acceptance prerequisites: an irrelevant result
does not get hidden as an adapter error. This does NOT test production neural FastEmbed/
Cross-Encoder quality, cross-language retrieval or full semantic RAG. Local Qdrant warns that
payload indexes have no effect; scope filters still execute and are checked.

Workflow and retrieval have separate task/metric denominators. Each completed run retains the
standard report plus `ablation_comparison.json`; delta direction is **variant minus baseline**.
Only identical case/repeat/seed, input/gold digests and resource identities are matched. Metric
differences use only pairs with both measurements and show paired/planned coverage; missing
measurements are unknown, not zero. These small controlled contrasts are descriptive, not
statistically significant evidence. Script callbacks and synthetic budget tokens are still
separate from the zero actual vendor tokens/API requests. Money remains unknown.

No Docker is needed for the retrieval suite:

```powershell
Set-Location D:\98281\deepscholar
.venv\Scripts\python.exe -m autoscholar.evaluation run --category rag --ablations --profile injected --repeats 2
if ($LASTEXITCODE -ne 0) { throw 'Local retrieval ablation checks failed' }
```

For workflow ablations, use the **complete isolated 10E startup/cleanup block above**, changing
the unique project prefix to `autoscholar-eval-10f-` and replacing its run command with:

```powershell
.venv\Scripts\python.exe -m autoscholar.evaluation run --category end_to_end --ablations --profile injected --sandbox-container $controller --timeout 360 --repeats 2
if ($LASTEXITCODE -ne 0) { throw 'Workflow ablation checks failed' }
```

Keep that block's pinned local context, explicit public defaults, cached-image/no-build flags,
and finally cleanup for only its unique project. Shared live cancellation/isolation checks
may also be rerun using the same controller; no normal app DB/migrations/stack are involved.
All databases, workspaces, artifacts and reports stay ignored under `data/`; secrets and design
documents must never be staged. Neither CLI enables paid providers or modifies production
policies: the ablation service subclass is confined to the explicit evaluation path.

2026-10-10 verification on clean source `f04a755`: all 30 actual Docker workflow contracts
passed (18 independently completed tasks, 10 fabricated-accuracy negatives and 2 explicit
No Replanning stops); all 20 actual local retrieval contracts passed. Every workflow contrast
has 6/6 matched/scored pairs. Both retrieval variants have chunk MRR 1.0 on this small closed
corpus; no ranking gain is claimed. Model/script callbacks total 212, synthetic budget units
636, actual vendor tokens/API requests zero. Backend regression: 611 passed / 3 skipped;
34 new ablation tests plus 43 existing E2E tests pass; Ruff and mypy (206 files) pass.
Local run directories (ignored, never uploaded):
`data/evaluation/eval-63ea730fb07c4afdbb50e4a4e51d72eb` (workflow),
`data/evaluation/eval-3ea5f5a2e22d426ab5335dbcfb33de52` (retrieval).
Temporary controller/network removal preserves cached images, MNIST, SQLite, artifacts and
reports. No application DB migration, paid request or main merge is part of this verification.

## Engineering acceptance pack (10G)

`python -m autoscholar.evaluation pack` executes a fixed serial pack of all six categories
plus workflow and actual local lexical retrieval ablations. The default one-repeat plan has
155 records (80 + 10 + 20 + 10 + 5 + 5 + 15 + 10); two repeats have 310 records. All suite
inputs, adapter identities and cached sandbox image/MNIST digests are preflighted before any
pack starts. A clean committed source is required. It never loads application `.env`/Settings,
provisions paid providers, executes benchmark source on the host or silently retries a run.

Use the **complete startup/finally-cleanup block in the E2E section above**, change the unique
project prefix to `autoscholar-eval-10g-`, and replace the category run command with:

```powershell
.venv\Scripts\python.exe -m autoscholar.evaluation pack --sandbox-container $controller --timeout 360
if ($LASTEXITCODE -ne 0) { throw 'Engineering acceptance pack failed; inspect its local reports' }
```

Keep pinned local context, explicit public defaults, cached-image/no-build flags and the two
real isolation/cancellation scripts. Only the unique temporary project is removed; reports,
artifacts, SQLite, public data and cached images are preserved. A longer local CPU run is
expected; it produces no external API requests. `--repeats`, `--seed`, `--timeout` and
`--output-root data/evaluation/<group>` remain explicit bounded options. There is no resume,
suite selection, provider toggle, input path override or replacement of missing Docker.

Each new ignored `data/evaluation/pack-<UUID>/` retains `pack_manifest.json`, `summary.json`,
the total `evaluation_report.md` and its eight runs under `runs/<slot>/eval-<UUID>/`.
The aggregator revalidates bounded primary manifest/JSONL bytes against the exact preflighted
suite, category, adapter/resource identities, committed source, case IDs, input/gold hashes,
repeat/seed and outcome/check consistency. Duplicate/mismatched records cannot pass. The
primary file digests are checked again at completion. Reports are local evidence, not signed
tamper-proof attestations; subrun summary/Markdown labels are never trusted as acceptance.

All eight planned suites remain in coverage after infrastructure errors/cancellation. Errors
stop without retry; cancellation keeps started primary records and partial total reports.
Skipped/unrun/unknown usage is not converted to zero. Checks and independent E2E completion
stay per suite/variant; relevance metrics, latency and paired ablation differences retain
their own measured/planned denominators. There is no mixed overall task-success rate or
statistical/neural/model-ability claim. Script callbacks/synthetic budget units are separate
from vendor tokens; monetary cost remains unknown without price evidence.

Exit 0 means the controlled **engineering pack** passed; 1 means incomplete/failed evidence,
2 means configuration/IO error, 130 means cancellation. `phase10_overall_acceptance` remains
false and `unverified` explicitly names production neural semantic retrieval and real
provider/model ability. A successful engineering pack does not approve a main merge or prove
full Phase 10 ability acceptance.

2026-10-10 verification on clean source `145af73`: the full eight-suite pack recorded and
passed all **155/155** engineering checks, with source unchanged throughout execution. E2E
independent task completion is 3/5; workflow baseline/No Planner/No Reviewer/No Memory each
complete 2/3, and No Replanning completes 1/3. Four workflow contrasts have 3/3 scored pairs;
the separate retrieval contrast has 5/5. Expected stops and fabricated-accuracy negatives
never earn task-completion credit. All 155 records measure zero external requests/vendor
tokens. There are 209 scripted/fixture calls, not paid visits. Only 20 workflow records expose
budget units (411 known synthetic units); the grand budget total remains unknown because
135 other records do not measure it. Monetary cost remains unknown without pricing evidence.
New pack-contract tests: 37 passed. Full backend: 648 passed / 3 skipped; Ruff, mypy (208 files)
and changed-file format checks pass. Skips remain the old Compose opt-in and two Windows
symbolic-link permission limitations, not these dedicated real sandbox checks.

Ignored local evidence, never uploaded:

- `data/evaluation/pack-660be828d0564c7d942fa019458eb1cd/evaluation_report.md` (total report;
  manifest, recomputed summary, primary hashes and all eight subruns retained beside it).
- `data/validation/phase10e-0b4e1ce0b3594760916dcd3bc9981523/acceptance.json` (actual running
  training cancellation, reopened DB, recovery required and no automatic replay).
- `data/validation/phase10d-7d0976389145410d80f5f6dbf8d8d0bf/acceptance.json` (actual sandbox
  isolation, timeout, cancellation and joined cleanup).

Both real supplemental checks used the same dedicated controller. Owned child containers
and volumes were zero before its removal; its controller/network were removed, preserving
cached images, public MNIST, reports, artifacts and SQLite. No application DB migrations,
normal deployment rebuild, paid API requests or main merge occurred.

## Actual cached neural retrieval validation

The separate `neural` command uses the actual production-default FastEmbed providers:
multilingual MiniLM (384 dimensions), `Qdrant/bm25`, and `BAAI/bge-reranker-base`.
It is not a ranking replay or the earlier lexical/Jaccard ablation. Explicit fake inference
in contract tests is labeled as such and never counts as a real-model result.

Requirements: clean committed source, local Docker running, an explicitly named existing
inference image with project dependencies, and an existing cache volume with all three
model snapshots. No image pull, model download, provider credentials or host inference
fallback occurs. Missing caches fail preflight. Normal application defaults are unchanged.

```powershell
.venv\Scripts\python.exe -m autoscholar.evaluation neural --image autoscholar-api:phase9-acceptance --cache-volume autoscholar_model_cache --repeats 2 --timeout 60
```

The fixed public inputs are `semantic/corpus_v1.json` (20 self-authored pre-chunked passages,
query bindings only) and `semantic/public_v1.json` (independent labels: 8 English, 8 Chinese,
4 cross-language queries). Labels are scoring-only, never model input. Fresh file SQLite
and local Qdrant run the real scoped RAG retrieval service. This is controlled pre-indexed
text, not PDF ingestion or an HTTP/deployment/generation evaluation.

Four modes run serially, with two repeats by default: 160 planned records. Dense/sparse
return five; both hybrid modes retrieve the same ten ordered candidates before returning
five. The reranker comparison checks per-query/seed candidate identity plus input/gold/
resource identities. Repeated fixed queries are not independent samples; paired deltas are
descriptive and do not assert reranking superiority or statistical significance.

Inference is in a CPU-only, network-none, non-root, read-only container, capabilities
dropped, no-new-privileges, bounded to 2 CPUs/6 GiB/128 PIDs. Only source and model cache are
mounted read-only; no Docker socket, application database, .env, design document, ports or
GPU. The worker verifies its restrictions before loading fixed local snapshots with
`local_files_only=True`. Actual snapshot file SHA-256, revisions, inference source, image
and runtime versions are recorded; source/cache are checked again and owned cleanup is
joined. Only this fresh UUID/ownership-labelled worker can be stopped.

Quality targets are frozen before inference: dense, hybrid and hybrid_rerank require overall
Hit@5 >= 0.85 and each English/Chinese/cross-language subset >= 0.75. Sparse/BM25 scores
are reported without a multilingual quality promise. Engineering checks and measured
Recall/Hit/MRR/NDCG are separate; low relevance remains low relevance. Missing metrics,
incomplete records, changed sources/resources or unverified cleanup cannot pass.

Ignored `data/evaluation/neural-<UUID>/eval-<UUID>/` contains normal primary manifest/JSONL,
summary/report plus `semantic_acceptance.json` and `inference_identity.json`. Primary records
are revalidated with the pack inspector. Cancellation retains partial records and planned
denominators; no automatic retry/resume. Latency excludes model warmup, corpus embedding,
indexing and shutdown. Local inference counts are separate from the zero vendor calls/tokens;
no paid price estimate is invented. Exit codes: 0 bounded engineering/semantic gate passed,
1 gate not accepted, 2 preflight/configuration failure, 130 cancellation.

Even a passing bounded semantic run leaves `phase10_overall_acceptance=false` and real paid
provider/model ability unverified. It does not grant permission for paid calls or a main merge.

2026-10-12 actual verification on clean source `7b52091`: all **160/160** engineering records
pass, and every frozen quality target passes. Two repeats of the same 20 queries are not 40
independent samples. Measured chunk ranking metrics:

| Mode | Overall Hit@5 | English Hit@5 | Chinese Hit@5 | Cross-language Hit@5 | Overall MRR |
|---|---:|---:|---:|---:|---:|
| dense | 1.0 | 1.0 | 1.0 | 1.0 | 0.975 |
| sparse/BM25 | 0.45 | 1.0 | 0.0 | 0.25 | 0.45 |
| hybrid | 1.0 | 1.0 | 1.0 | 1.0 | 0.933333 |
| hybrid_rerank | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |

Coverage is 40/40 overall, 16/16 English, 16/16 Chinese, 8/8 cross-language for each mode.
Same-pool reranker contrasts have 40/40 scored pairs: MRR delta +0.066667, Recall@5 delta
0, NDCG@5 delta +0.05; descriptive only. The sparse-language limitation is retained rather
than concealed by the engineering pass. Actual runtime versions are FastEmbed 0.8.0 and
ONNX Runtime 1.29.0. The model snapshots and source are unchanged after inference; owned
container cleanup is verified. Corpus batches: one dense and one sparse; benchmark query
operations: 120 dense, 120 sparse, 40 reranks, plus one readiness operation of each kind.
These are local inference operations, not vendor visits. Actual vendor calls/tokens are 0.

41 new contract tests pass; full backend 689 passed / 3 skipped, Ruff, mypy (213 files),
and 33 changed/evaluation files' format checks pass. Skips remain the old Compose opt-in
and two Windows symbolic-link limitations. Additional actual model-RPC cancellation and
timeout checks, sent while waiting for a response, propagate the exceptions, join owned
cleanup and reject further calls on the closed client. No automatic retry, paid requests,
normal deployment rebuild, application database migration or main merge occurred.

Ignored local evidence, never uploaded:

- `data/evaluation/neural-1c60445a1d554ed08d7a0c9765df295d/eval-e8d1bf658c694d1ca62cfb3e6f0ef788/evaluation_report.md`
  (primary manifest/JSONL, semantic gate, full runtime/model identities and summaries beside it).
- `data/validation/phase10h-9725d9b964fa4bffa6623269ff9918ee/acceptance.json`
  (actual cached model-RPC cancellation and timeout, isolation, joined cleanup, source identity).

## Bounded real-provider verification

`providers` is a separate paid entry point; offline/pack/neural defaults never enable it.
Require a clean committed source, a unique dedicated controller from the 10D setup above,
configured LLM/Tavily, and an explicit one-shot approval. No model migration or normal
application/default-provider changes occur. The fixed questions and independent graders
are versioned in `evaluation/provider_cases.py`; mock-transport tests are not live evidence.

```powershell
# This makes paid requests. Obtain owner approval first; no grant is inferred from old phases.
.venv\Scripts\python.exe -m autoscholar.evaluation providers --allow-paid --approval-id phase10-approved-one-shot --sandbox-container $controller
```

Limits: ten LLM HTTP attempts, two three-result basic Tavily searches, 32 KiB per public
request, 128 KiB per response, and `max_tokens=4096` for short completions. Use the configured
model unchanged. HTTPS endpoints are fixed, no redirects/proxy environment/automatic SDK,
HTTP or search retries. The ledger reserves each physical attempt BEFORE forwarding it;
duplicate case dispatch and reusing the same approval ID cannot reset its allowance.
Only selected local settings configure authentication/model; keys stay in auth headers,
never JSON, prompts, reports or Git. No private design, .env content or application DB is sent.

The ten contracts cover planning/public memory constraints, native Calculator/tool result,
code generation and repair, invalid accuracy review, experiment-only replanning, English
and Chinese retrieved-source answers, and latest-approved-result writing. Production
Provider/Calculator/DAG/Review/Revision validation is used. Code is AST-scoped then statically
checked and graded against eight independently held-out vectors in real Docker; no generated
code runs on the host. Citations must bind exact actual excerpt substrings and support the
fixed public fact, not merely have a valid ID. These are bounded per-component provider
capabilities, NOT a fully autonomous paid DurableService/Research/Coding/Experiment run.

Each case has one fixed sample, no repeats or automatic repairs; dependency failure stays
skipped with the original planned denominator. Provider/infrastructure errors stop dispatch
and preserve partial reports; no automatic rerun/resume. Actual request counts and vendor
usage come from the guarded transport, not fixtures. Missing token usage and monetary cost
without vendor price evidence remain unknown. Rejecting invalid metrics is not task completion.

Ignored `data/evaluation/provider-<UUID>/` retains manifest, cases, redacted public request/
response snapshots, actual evidence, request ledger, `provider_acceptance.json`, and
`evaluation_report.md`. One-shot grants are in ignored `provider-approvals/`. Exit 0 only
passes these bounded capability contracts; it neither asserts general model ability nor
automatically sets overall Phase 10 acceptance or approves a main merge.

2026-10-12 partial 10I verification: 35 new mock-transport contracts pass; the complete
backend on clean source `d45261b` has 724 passed / 3 skipped. Ruff, mypy (217 files) and 37
evaluation/changed files' formatting pass. Actual isolated Docker oracle preflight accepts
the correct unique-order function and rejects the sorted-set implementation. Neither oracle
preflight used a vendor; both joined child container/volume cleanup.

The explicitly approved live run issued **2 LLM attempts, 0 Tavily attempts**. Planning passed
the actual production DAG schema, dependency checks and public-memory constraints; the
returned model name is `deepseek-flash` while the configured alias remains unchanged.
The native-tool request returned HTTP 400 immediately, and dispatch stopped without retry.
The successful planning response reports 1387 tokens; the failed request has unknown usage,
so total vendor tokens and monetary cost remain unknown. Eight unexecuted contracts remain
in the original ten-case denominator. Bounded provider acceptance is **false**.

The initial evaluator forced a named calculator tool, unlike production Agent `auto` mode.
[DeepSeek's API reference](https://api-docs.deepseek.com/api/create-chat-completion/) states
that default thinking mode rejects named/required tool choice with HTTP 400. This is
consistent with the observed request/status; the raw error body was not exported, so it is
not proof that production auto-tool calling fails. The evaluator now uses production-aligned
`auto`; actual native calls and independently calculated values are still required for credit.
The model/thinking mode and graders are unchanged. An added 400-stop contract brings new
tests to 36 and focused Provider/Search/Research regression to 61 passed; Ruff/mypy pass.
This fix has **not** been verified with another paid request. The original grant cannot be
reset/reused, no failed case was rerun, and first-failure evidence is not rewritten.

Ignored local report:
`data/evaluation/provider-d44b278800bd4b4d9afbb6fc27b13439/evaluation_report.md`
(manifest, original public request payloads, planning response, cases and request ledger
retained beside it). All owned children/volumes were zero before removing the dedicated
controller/network. Cached images, public MNIST, application data and reports remain.
No normal deployment rebuild, application DB migration, private-file upload or main merge.

A manually authorized follow-up can reuse the already-verified planning evidence, retest
the failed tool contract and execute the eight unexecuted contracts: at most nine new LLM
and two Tavily attempts. Together with the original two, that would be thirteen attempts,
one beyond the original twelve-request allowance; explicit new approval is required.

### Explicit manual follow-up with verified planning reuse

After new owner approval, `--prior-run` can complete the remaining contracts with at most
nine new LLM and two Tavily attempts. It does not resume/reset the original grant. Example:

```powershell
.venv\Scripts\python.exe -m autoscholar.evaluation providers --allow-paid --approval-id phase10-manual-followup --prior-run data/evaluation/provider-d44b278800bd4b4d9afbb6fc27b13439 --sandbox-container $controller
```

Before dispatch, it bounds/confines reads to the exact public prior run, verifies the original
manifest/Git grader source, matching suite/oracle/model/endpoint, two returned HTTP attempts
(planning 200, tool 400), original approval ledger, request-body and response/case hashes,
and independently regrades planning. The reused record retains its original run/revision
and is NOT a fresh model sample or new request. No failed record is overwritten.

The new one-shot ledger blocks planning and enforces 9/2; an exclusive claim on the prior
run prevents a fresh approval ID from silently resetting cumulative allowance. Current and
cumulative usage are separate; original failed-request tokens remain unknown. Prior source
snapshots/ledger hashes are rechecked during/after execution. Tool-result messages preserve
the original public question and actual model-generated call, including reasoning metadata.
No model, thinking-mode, default app behavior or grading threshold is changed.

For an explicitly continued stopped follow-up, `--continue-unattempted` retains the same
grant/counters and only dispatches the seven still-unattempted independent contracts. It
verifies the exact stopped run, consumed native HTTP-200 reservation, source/model/endpoint,
original ownership claim and old planning proof. The native response was unavailable to the
grader; it stays error, its dependent result case stays skipped. No inferred calculator call,
synthetic model result, paid retry or allowance reset occurs. A fresh continuation claim is
exclusive; another CLI invocation cannot repeat it. This mode does not itself pass the full gate.

The response guard handles decompressed bytes with corrected encoding/length headers;
otherwise HTTPX may decode them again and report a local connection failure after HTTP 200.
Bounded credential-checked raw JSON snapshots are saved on successful responses before SDK
parsing. The old missing response is NOT retroactively reconstructed. Use short unique pytest
basetemp paths on Windows (`data/validation/p10-<short random ID>`); deeply nested pack report
paths can otherwise exceed local path limits. Preserve original failed evidence and do not
change OS settings or clear existing application data just to run this test.

2026-10-12 manual follow-up evidence: the prior planning request/response, original grant,
source and grading identity were verified; no planning request was sent again. The new
native request returned HTTP 200 and 483 measured tokens, but the client response wrapper
could decode an already-decompressed body a second time because encoding headers were
retained. The SDK reported a local connection failure and the full answer was not saved;
HTTP status alone cannot grade the tool. The free fix removes stale encoding/length headers
and captures bounded credential-checked JSON before SDK parsing. The missing native output
is not reconstructed, and no automatic native retry occurred.

The explicit unused-allowance continuation on clean source `a3aea04` issued seven independent
LLM requests and two Tavily searches. Both code cases passed real static validation and all
eight held-out Docker checks each; metric-review, experiment-only replanning, English
source-bound answering and latest-result writing passed. Both Tavily requests returned 200.
Frozen grading is **7/10**, including one reused planning case, one native error, its skipped
dependent case and one Chinese format refusal. The provider gate remains false.

Saved Chinese output states the correct in-place/None facts, and all three quotes match
actual excerpts and support both claims. The original schema rejected textual `"None"`
(rather than JSON null) and two distinct quotes referencing E1 (rather than unique source
IDs). A separate offline diagnostic records these distinctions without changing the raw
response, failed score or frozen grader; no paid repeat or post-hoc pass is claimed.

Cumulative physical attempts: **10 LLM + 2 Tavily = 12**, within the newly approved cap of
13. This turn issued ten attempts including the missing native response; the continuation
alone issued nine. This turn's returned usage sums to 9687 tokens; cumulative known usage
is 11074, but the first HTTP 400 has unknown usage, so cumulative total tokens and money
remain unknown. Source and original proof stayed unchanged during real execution.

Full free backend: **740 passed / 3 skipped**; Ruff/mypy (218 files) pass. Provider contracts
51 plus pack tests 37 total **88 passed**. Seven initial pack failures were Windows path
length errors (expected deep report path 264 characters); shorter unique basetemp paths
passed all cases, without OS changes or deleting failed evidence. Skips remain old Compose
opt-in and two Windows symbolic-link restrictions. The dedicated controller/network and
all child containers/volumes are removed; cached images, public MNIST, app data and reports
remain. No application DB migration, normal deployment rebuild, private-file upload or main
merge occurred.

Ignored local evidence:

- `data/evaluation/provider-6db601fc4ca34752b256271c5bf80445/evaluation_report.md`
  (continuation manifest, real responses/search snapshots, cases and retained usage).
- Same directory's `research_chinese_diagnostic.json` (offline factual/quote checks vs frozen
  null/unique-source-ID rules, no change to the failed result).
- `data/evaluation/provider-001af831d0024f189706436ee4ad233f/` (original HTTP-200 native
  reservation, unavailable response, usage and partial failure preserved).

Overall Phase 10 is not accepted. Native/result confirmation needs explicitly authorized
paid work; Chinese format expectations must be clarified without concealing the first score.

### Separately approved tool-only confirmation

`providers --confirm-tools-from <completed-continuation> --prior-run <original-run>` needs
separate owner approval and a fresh one-shot approval ID. It binds the completed continuation,
old stopped follow-up, original planning proof, source/grader/model/endpoint, public request
and successful response hashes, ownership claims and consumed grant. Limits are **2 LLM / 0
Tavily**, blocked case IDs prohibit planning and all other contracts; the prior grant is not
continued or reset. An exclusive claim also prevents a different ID from rerunning the same
confirmation. Exact original Calculator questions, real tool output and reasoning metadata
are retained. If the first request fails, the dependent request is not sent; no paid retry.

Current component results and all historical attempts are reported separately, with cumulative
unknown usage preserved. `tool_chain_confirmation_acceptance` is not the full provider or
Phase 10 gate: original native failures, skipped result, Chinese format failure and old scores
remain immutable. This mode does not regrade Chinese facts or change any frozen rubric.

2026-10-12 authorized tool-only execution on clean `32ebe31`: **2/2 passed**, two LLM
HTTP-200 attempts, zero Tavily attempts, **692 measured tokens**. The actual model issued
one native Calculator call for `(17*23)+(9**2)`; the production tool returned `472`, and
the second request bound the real call ID/output plus reasoning metadata and original
question. The real answer was `{"result":472}`. No planning, other independent contract,
Chinese answer or search was repeated. The original failed scores remain unchanged.

Cumulative **12 LLM + 2 Tavily = 14 attempts**, exactly the separately approved ceiling;
known vendor tokens **11766**. Initial HTTP-400 usage remains unknown, so cumulative total
tokens and monetary cost are unknown. Source, prior proof and consumed old grants stayed
unchanged. The new component gate is true, full provider and Phase 10 gates remain false.
The outstanding frozen Chinese format contract has not been changed or rerun.

Free backend **752 passed / 3 skipped**, provider contracts **63 passed**, Ruff/mypy (219
files) and formatting pass. Preflight verified the dedicated controller with pinned cached
images, public dataset and no owned children. The controller/network were removed after
ownership-label checks; no application container/data/cache or report was removed.
Ignored local report: `data/evaluation/provider-13fc74e120364d2eba84f8f868f79a00/evaluation_report.md`.
The example approval ID is consumed; do not change IDs to rerun or reset the allowance.

## Explicit Chinese v2 format and historical closure audit

The original v1 questions, `provider_cases.py`, strict Chinese schema, fact/quote/support
grader, failed answer and 7/10 score are unchanged. `provider_chinese.py` adds a separately
versioned prompt only: native JSON null, unique source IDs, continuous exact source quotes,
no extra fields/Markdown. The schema is generated from the frozen `ChineseAnswer` model.
No old answer normalization, citation deletion or post-hoc v1 regrading occurs.

`chinese` defaults to free offline preparation. It does not read `.env`, create a network
client, start Docker or call search. Original planning, independent follow-up, confirmed
native request/raw response, reasoning/call IDs/arguments and actual Calculator output are
bound and rechecked. JSON argument whitespace is not a semantic mismatch. The existing real
public Python excerpts are reused; preparation remains `awaiting_approval`, not live evidence.

Only separate owner approval plus `--allow-paid --approval-id <new-public-ID>` permits the
single new LLM request. Limits are **1 LLM / 0 Tavily**, unchanged model/endpoint, no SDK or
transport retry, no implicit allowance continuation. An exclusive historical-tool-run claim
blocks a second ID from repeating the same v2 confirmation. Missing/failed responses and
unknown tokens/costs remain unaccepted/unknown. Existing grants and raw failures stay intact.
This new grant would raise the cumulative physical request cap from 14 to 15; preparation or
an instruction to fix code alone does not authorize that additional paid request.

`closure` is an offline report entry point. It revalidates engineering primary manifests and
case JSONL, dataset/seed/adapters, paired ablations, cached-neural primary records and runtime
model identities, and recomputes recorded semantic targets rather than trusting sidecar
pass flags. It binds provider history and independently verifies the new Chinese response
against its request, versioned prompt, cached evidence, grant, raw body and frozen grader.
Its evidence-only adapters reject execution. No API, inference, training or .env loading.

Both commands take `--original-run`, `--independent-run`, `--tool-run`. Closure also needs
`--engineering-run`, `--semantic-run`; optional `--chinese-run` points to a prepared or
completed v2 directory. Omission leaves Chinese pending, never retries it. Fresh local outputs
are `data/evaluation/chinese-v2-<UUID>/` and `closure-<UUID>/`, never uploaded.

The declared gate `bounded_phase10_closure_acceptance` requires all recorded engineering,
bounded semantic and provider coverage (nine v1 slots plus one new v2 Chinese slot). Original
v1 scores remain separately visible; this mixed-version/reused coverage is not a new full-suite
sample. `phase10_overall_acceptance`/`main_merge_authorized` stay false: autonomous paid E2E
and real-model statistical ablations remain unverified and require a separate scope/approval.
Exit 0 for closure means only that bounded scope passed, 1 incomplete/failed, 2 invalid evidence.

Free verification found a pre-existing ablation-observation defect: SQLite initial/revised
plans can share a second-resolution timestamp, and UUID tie order can put version 2 first.
The failing local evidence had version 2/revision before version 1/static at the same time;
the isolated case passed, but the defect was fixed rather than hidden by a rerun. The
evaluation-only initial-plan observer now selects the minimum version, not list position.
Four deterministic tie/order/non-static-initial tests cover both directions. No normal DB,
repository history ordering, v1 provider grader or old evidence was modified. New Chinese/
closure contracts: 27; focused provider/ablation/closure regression: **128 passed**.

2026-10-12 free closure verification: complete backend **783 passed / 3 skipped**; Ruff,
mypy (223 files) and 42 evaluation/related test files' formatting pass. The first full run's
timestamp-tie failure remains in its local validation directory; the deterministic fix was
followed by a passing full regression. Original frozen provider source/digests remain unchanged.

Actual offline preparation/closure on clean `9c800c8` revalidated **155/155** engineering
primary records, passing bounded cached-neural quality, and verified **9/10** provider-slot
coverage. The original v1 score remains **7/10**; Chinese v2 is pending. This turn issued
**0 new vendor requests**, cumulative still **14**, and no new paid approval ledger exists.
No Docker startup, normal deployment/DB change or private-file upload occurred.

Ignored local preparation: `data/evaluation/chinese-v2-e86621708b0f44dab62ffc1e463aa87a/`.
Ignored local report: `data/evaluation/closure-122e7f62c1034131a172f7aa576a98be/evaluation_report.md`.
Old proof/grants stayed unchanged; bounded closure/full Phase 10/main-merge flags remain
false. The next new-format confirmation needs a separately authorized **one LLM / zero
Tavily** request, raising the cumulative ceiling to 15; no approval is inferred from this fix.

2026-10-12 separately authorized v2 live confirmation on clean `cc537b5`: **one LLM / zero
Tavily** attempt, HTTP 200, all six checks pass (native JSON null, strict schema, unique
source IDs, independent fact, exact quote binding, source support). The model used a single
E2 excerpt supporting in-place sorting and Python None. Existing model/endpoint/default
thinking mode and original frozen grader are unchanged. No other contract or search rerun.

New measured usage: **1588 input + 648 output = 2236 tokens**. Cumulative physical attempts:
**13 LLM + 2 Tavily = 15**, exactly the newly approved cap, no automatic retry. Known tokens:
**14002**; cumulative total and money remain unknown because the first HTTP 400 omitted
usage and no price basis is supplied. Old failures, grants and source evidence remain intact.

The new offline closure revalidates primary engineering/semantic/provider evidence and passes
`bounded_phase10_closure_acceptance` under `engineering_and_bounded_capabilities_v1`:
**10/10 coverage, no remaining bounded gaps**. Original v1 score is still **7/10**; nine slots
are verified v1 evidence, one is a new-prompt v2 sample, not a fresh full-suite measurement.
`phase10_overall_acceptance` and `main_merge_authorized` remain false. Autonomous paid E2E
and real-model statistical ablations remain outside this validated scope. No further paid
request or main merge is authorized; the 15-attempt grant is exhausted.

Ignored local evidence:

- `data/evaluation/chinese-v2-29ccb291b1e6441fbeabec76d259e54d/evaluation_report.md`.
- `data/evaluation/closure-51779bc514214d148fa4ec43eecfe425/evaluation_report.md`.

No Docker startup, normal deployment/DB mutation, private design/.env upload or raw-evidence
publication occurs. Final audit checks the new grant/request hash, old source/failure retention,
coverage, cumulative ceiling and separate overall/main flags without extra model requests.

Post-confirmation free provider/closure regression **90 passed**; Ruff/mypy (223 files)
pass. No source implementation changed this turn; the prior complete source regression
remains **783 passed / 3 skipped**, not claimed as another full rerun. Only public result
documentation is committed; all primary evidence and private configuration stay ignored.

## Final free ablation review

`ablation-audit` revalidates the original engineering pack and cached-neural primary records,
then computes post-hoc paired reranker statistics. It creates no HTTP clients, reads no `.env`,
and performs no inference, training or provider calls. Source must be clean and committed.

```powershell
.venv\Scripts\python.exe -m autoscholar.evaluation ablation-audit --engineering-run data/evaluation/pack-660be828d0564c7d942fa019458eb1cd --semantic-run data/evaluation/neural-1c60445a1d554ed08d7a0c9765df295d/eval-e8d1bf658c694d1ca62cfb3e6f0ef788
if ($LASTEXITCODE -ne 0) { throw 'Ablation evidence/statistics review failed' }
```

The 40 paired repeat records are collapsed to 20 query means. Connected components of shared
gold documents form 16 blocks, including transitive multi-document overlap. Delta direction
is **hybrid_rerank minus hybrid**. The fixed family is chunk MRR, Recall@5 and NDCG@5: report
query-weighted mean deltas, 4,096 document-block bootstrap resamples (seed 20261013), percentile
95% intervals, exact two-sided block sign-flip p-values and Holm correction at alpha 0.05.
Intervals assume exchangeable blocks; sign flips assume symmetric null effects. Shared-source
grouping does not establish independent blocks, and neither method establishes generalization
from this fixed self-authored corpus. This is **post-hoc, not preregistered**; no new independent
samples are added. Missing/invalid pair metrics prevent complete statistical acceptance.

Primary inputs, gold labels, seeds, models, resources and recorded candidate-pool checks are
validated before pairing. Original file hashes are checked again before writing the report.
Local evidence remains unsigned; hash consistency is not external attestation. No gain or
statistical nonsignificance is a valid experimental outcome, not an infrastructure failure.
Exit 0 means this evidence/statistics review passed, not that reranking is beneficial.

The ignored `data/evaluation/ablation-audit-<UUID>/` output contains JSON and Markdown.
It separately lists historical scripted workflow interventions and marks real-LLM workflow
ablations and autonomous real-LLM E2E unverified. Overall Phase 10 and main authorization
remain false. This command does not renew or spend any previous paid allowance.

## Next stages

Engineering, bounded default-cache neural retrieval and the mixed-version bounded provider
closure now have passing evidence, including separately approved tool and Chinese v2
confirmations. Original frozen v1 failures remain visible and are not retroactively passed.
Autonomous real-LLM E2E and real-LLM workflow ablations remain unverified; any additional
paid request or main merge needs separate owner approval. Broader corpora,
PDF ingestion/generation quality and independent paid-provider ability remain outside this
result. The generic injected RAG adapter accepts an explicitly provisioned
retriever; it does not create one from environment settings and does not infer its API cost.
Real-model semantic comparisons require actual labeled corpus retrieval, actual model/resource
identities and separate verification. Real paid-provider evaluations require owner approval.
