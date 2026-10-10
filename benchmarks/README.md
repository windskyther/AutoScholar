# AutoScholar-Eval benchmark contracts

Versioned suites separate `inputs` from `expected` gold labels. Execution adapters receive only
the prompt and inputs. All included Phase 10A/10B/10C/10D/10E cases are self-authored public engineering
fixtures; no provider requests, private design documents, credentials or runtime records are
included. The CLI implements RAG replay, controlled Research/Tool component execution and
explicitly injected real Docker Coding/Experiment and durable E2E execution. Ablations remain unfinished.

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

## Next stages

Independent neural semantic/provider verification remains separate Phase 10 work after the
controlled 10G engineering pack. The generic injected RAG adapter accepts an explicitly provisioned
retriever; it does not create one from environment settings and does not infer its API cost.
Real-model semantic comparisons require actual labeled corpus retrieval, actual model/resource
identities and separate verification. Real paid-provider evaluations require owner approval.
