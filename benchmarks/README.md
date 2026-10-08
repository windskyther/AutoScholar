# AutoScholar-Eval benchmark contracts

Versioned suites separate `inputs` from `expected` gold labels. Execution adapters receive only
the prompt and inputs. All included Phase 10A/10B/10C/10D cases are self-authored public engineering
fixtures; no provider requests, private design documents, credentials or runtime records are
included. The CLI implements RAG replay, controlled Research/Tool component execution and
explicitly injected real Docker Coding/Experiment execution. E2E and ablations remain unfinished.

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

## Next stages

E2E, ablation and independent real semantic/provider verification
remain separate Phase 10 modules. The injected RAG adapter accepts an explicitly provisioned
retriever; it does not create one from environment settings and does not infer its API cost.
Real-model semantic comparisons require actual labeled corpus retrieval, actual model/resource
identities and separate verification. Real paid-provider evaluations require owner approval.
