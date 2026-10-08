# AutoScholar-Eval benchmark contracts

Versioned suites separate `inputs` from `expected` gold labels. Execution adapters receive only
the prompt and inputs. All included Phase 10A/10B cases are self-authored public engineering
fixtures; no provider requests, private design documents, credentials or runtime records are
included. Other evaluation categories are planned, not implemented by the CLI yet.

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
are rejected before starting a run. This CLI supports only `--profile offline` and does not
load `.env`, provision models, connect to a database, or accept dynamic execution plugins.

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

## Next stages

Research/Tool, Coding/Experiment, E2E, ablation and independent real-component verification
remain separate Phase 10 modules. The injected RAG adapter accepts an explicitly provisioned
retriever; it does not create one from environment settings and does not infer its API cost.
Real-model semantic comparisons require actual labeled corpus retrieval, actual model/resource
identities and separate verification. Real paid-provider evaluations require owner approval.
