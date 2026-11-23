"""Serial, bounded evaluations with separate execution inputs and gold labels."""

import asyncio
import json
import platform
import subprocess
import time
from collections.abc import Sequence
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import JsonValue

from autoscholar.evaluation.datasets import payload_digest
from autoscholar.evaluation.models import (
    AdapterIdentity,
    BenchmarkCase,
    BenchmarkSuite,
    CaseReason,
    CaseResult,
    CaseStatus,
    MeasuredUsage,
    Observation,
    RunConfiguration,
    RunManifest,
    ScoreCard,
)


class EvaluationAdapter(Protocol):
    identity: AdapterIdentity

    def validate_case(self, case: BenchmarkCase) -> None: ...

    async def execute(
        self, prompt: str, inputs: dict[str, JsonValue], *, seed: int
    ) -> Observation: ...

    def score(self, observation: Observation, expected: dict[str, JsonValue]) -> ScoreCard: ...


def _write_json(path: Path, payload: object) -> None:
    # Files are owned by this new run. Atomic replacement does not overwrite other runs.
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")
    temporary.replace(path)


def _git_state(repo_root: Path) -> tuple[str | None, bool | None]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None, None
    return revision, bool(dirty)


async def _case(
    adapter: EvaluationAdapter, case: BenchmarkCase, configuration: RunConfiguration, repeat: int
) -> CaseResult:
    started = time.perf_counter()
    observation: Observation | None = None
    score: ScoreCard | None = None
    status: CaseStatus = "error"
    reason: CaseReason | None = "adapter_error"
    seed = (configuration.seed + repeat - 1) % (2**32)
    try:
        # Gold labels are never passed to the execution method. Per-run copies prevent
        # adapters from modifying the dataset or contaminating later variants/repeats.
        async with asyncio.timeout(configuration.case_timeout_seconds):
            observation = Observation.model_validate(
                await adapter.execute(case.prompt, deepcopy(case.inputs), seed=seed)
            )
            score = ScoreCard.model_validate(adapter.score(observation, deepcopy(case.expected)))
            if score.task_success is not None and adapter.identity.category != "end_to_end":
                raise ValueError("Task success is an E2E metric")
            status = "passed" if all(score.checks.values()) else "failed"
            reason = None if status == "passed" else "checks_failed"
    except TimeoutError:
        status, reason = "timeout", "timeout"
    except asyncio.CancelledError:
        status, reason = "cancelled", "cancelled"
    except Exception:
        # Exception messages and traceback payloads may contain prompts/secrets. Never
        # put them in exported evaluation files or console output.
        status, reason, score = "error", "adapter_error", None
    return CaseResult(
        case_id=case.id,
        category=adapter.identity.category,
        adapter=adapter.identity,
        repeat=repeat,
        seed=seed,
        status=status,
        reason=reason,
        duration_ms=round((time.perf_counter() - started) * 1000, 3),
        score=score,
        usage=observation.usage if observation else MeasuredUsage(),
        input_sha256=payload_digest({"prompt": case.prompt, "inputs": case.inputs}),
        expected_sha256=payload_digest(case.expected),
    )


async def run_evaluation(
    suite: BenchmarkSuite,
    *,
    dataset_sha256: str,
    adapters: Sequence[EvaluationAdapter],
    configuration: RunConfiguration,
    output_root: Path,
    repo_root: Path,
) -> Path:
    if not adapters or len(adapters) > 10:
        raise ValueError("Select between 1 and 10 adapters")
    if len({adapter.identity.variant for adapter in adapters}) != len(adapters):
        raise ValueError("Adapter variant IDs must be unique")
    for adapter in adapters:
        if configuration.profile == "offline" and adapter.identity.execution != "fixture":
            raise ValueError("Offline runs only accept known fixture adapters")
        if adapter.identity.category != suite.category:
            raise ValueError("Adapter and dataset categories differ")
        for case in suite.cases:
            adapter.validate_case(case.model_copy(deep=True))
    run_id = "eval-" + uuid4().hex
    # Always create a new directory; never resume/overwrite an existing run implicitly.
    destination = output_root / run_id
    destination.mkdir(parents=True, exist_ok=False)
    revision, dirty = _git_state(repo_root)
    manifest = RunManifest(
        run_id=run_id,
        started_at=datetime.now(UTC).isoformat(),
        suite_id=suite.id,
        dataset_sha256=dataset_sha256,
        configuration=configuration,
        adapters=[adapter.identity for adapter in adapters],
        git_revision=revision,
        git_dirty=dirty,
        python_version=platform.python_version(),
        platform=platform.system(),
    )
    _write_json(destination / "manifest.json", manifest.model_dump(mode="json"))
    results: list[CaseResult] = []
    try:
        with (destination / "cases.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
            for adapter in adapters:
                for repeat in range(1, configuration.repeats + 1):
                    for case in suite.cases:
                        result = await _case(adapter, case, configuration, repeat)
                        results.append(result)
                        stream.write(result.model_dump_json() + "\n")
                        stream.flush()
                        if result.status == "cancelled":
                            raise asyncio.CancelledError()
        manifest.state = "completed"
    except asyncio.CancelledError:
        manifest.state = "cancelled"
        raise
    except Exception:
        manifest.state = "error"
        raise
    finally:
        manifest.completed_at = datetime.now(UTC).isoformat()
        _write_json(destination / "manifest.json", manifest.model_dump(mode="json"))
        from autoscholar.evaluation.report import write_report

        write_report(destination, manifest, results, len(suite.cases) * configuration.repeats)
    return destination
