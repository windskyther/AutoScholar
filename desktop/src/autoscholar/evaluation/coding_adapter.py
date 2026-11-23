"""Real CodingAgent validation/repair plus independent, hidden Docker oracle cases."""

import hashlib
import json
from collections import deque
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from autoscholar.coding.agent import CodingAgent, CodingLimits, CodingRunError
from autoscholar.coding.sandbox import SandboxExecutor, SandboxRunRequest
from autoscholar.evaluation.component_fixture import QueryInputs, VersionedFixture, fixture_usage
from autoscholar.evaluation.isolated_components import (
    ObservedSandbox,
    local_component_task,
    oracle_json,
)
from autoscholar.evaluation.models import (
    AdapterIdentity,
    BenchmarkCase,
    EvaluationModel,
    Identifier,
    Observation,
    ScoreCard,
)
from autoscholar.llm.models import (
    ConversationMessage,
    LLMResult,
    TokenUsage,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)

CodingStatus = Literal["accepted", "agent_failed", "oracle_rejected"]
CodingError = Literal["code_repair_exhausted", "oracle_mismatch"]


class CodingScript(EvaluationModel):
    prompt: str = Field(min_length=1, max_length=10000)
    initial_files: dict[str, str] = Field(min_length=1, max_length=6)
    repaired_files: dict[str, str] = Field(default_factory=dict, max_length=6)
    max_repairs: int = Field(default=1, ge=0, le=1)

    @model_validator(mode="after")
    def bounded_files(self) -> "CodingScript":
        for files in (self.initial_files, self.repaired_files):
            SandboxRunRequest(task_id="preflight", action="static_check", files=files)
            if any(len(text.encode()) > 65536 for text in files.values()):
                raise ValueError("Coding fixture source exceeds 64 KiB")
        if "solution.py" not in self.initial_files:
            raise ValueError("Coding fixture must author solution.py")
        if not self.repaired_files.keys() <= self.initial_files.keys():
            raise ValueError("Repair fixtures may only edit authored files")
        return self


class CodingFixture(VersionedFixture):
    scripts: dict[Identifier, CodingScript] = Field(min_length=1, max_length=1000)


class OracleCheck(EvaluationModel):
    args: list[JsonValue] = Field(max_length=8)
    kwargs: dict[str, JsonValue] = Field(default_factory=dict, max_length=8)
    expected: JsonValue = None
    raises: Literal["ValueError", "TypeError", "ZeroDivisionError"] | None = None


class CodingOracle(EvaluationModel):
    prompt: str = Field(min_length=1, max_length=10000)
    function: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    checks: list[OracleCheck] = Field(min_length=3, max_length=30)

    @model_validator(mode="after")
    def bounded_vectors(self) -> "CodingOracle":
        if len(self.model_dump_json().encode()) > 65536:
            raise ValueError("Coding oracle vectors exceed 64 KiB")
        return self


class CodingOracleFixture(VersionedFixture):
    cases: dict[Identifier, CodingOracle] = Field(min_length=1, max_length=1000)


class OracleResult(EvaluationModel):
    schema_version: int = Field(ge=1, le=1)
    passed_count: int = Field(ge=0, le=30)
    total_count: int = Field(ge=3, le=30)

    @model_validator(mode="after")
    def count(self) -> "OracleResult":
        if self.passed_count > self.total_count:
            raise ValueError("Oracle counts are inconsistent")
        return self


class CodingLabels(EvaluationModel):
    status: CodingStatus
    error_code: CodingError | None = None
    compile_passed: bool
    tests_passed: bool
    repairs: int = Field(ge=0, le=1)

    @model_validator(mode="after")
    def status_error(self) -> "CodingLabels":
        if (self.status == "accepted") != (self.error_code is None):
            raise ValueError("Coding rejection needs an expected error")
        return self


class CodingObservation(EvaluationModel):
    status: CodingStatus
    error_code: CodingError | None = None
    compile_passed: bool
    tests_passed: bool
    agent_completed: bool
    repairs: int = Field(ge=0, le=1)
    oracle: OracleResult | None = None
    sandbox_runs: int = Field(ge=0, le=8)


def score_coding(observation: Observation, expected: dict[str, JsonValue]) -> ScoreCard:
    labels = CodingLabels.model_validate(expected)
    actual = CodingObservation.model_validate(observation.payload)
    functional = (
        actual.oracle.passed_count / actual.oracle.total_count
        if actual.oracle is not None
        else None
    )
    accepted = (
        actual.status == "accepted"
        and functional == 1.0
        and actual.agent_completed
        and actual.compile_passed
        and actual.tests_passed
    )
    checks = {
        "coding_outcome": actual.status == labels.status,
        "error_classification": actual.error_code == labels.error_code,
        "compile_outcome": actual.compile_passed == labels.compile_passed,
        "pytest_outcome": actual.tests_passed == labels.tests_passed,
        "repair_count": actual.repairs == labels.repairs,
    }
    if labels.status == "accepted":
        checks["independent_oracle"] = accepted and actual.agent_completed
    elif labels.status == "oracle_rejected":
        checks["deceptive_self_tests_detected"] = (
            actual.agent_completed and functional is not None and functional < 1.0
        )
    return ScoreCard(
        checks=checks,
        metrics={
            "compile_success": float(actual.compile_passed),
            "pytest_success": float(actual.tests_passed),
            "agent_completion": float(actual.agent_completed),
            "independent_check_fraction": functional,
            "functional_correctness": float(functional == 1.0) if functional is not None else None,
            "repair_success": float(accepted) if actual.repairs else None,
            "repair_attempts": float(actual.repairs),
            "sandbox_runs": float(actual.sandbox_runs),
            "coding_acceptance": float(accepted),
            "oracle_rejection": float(actual.status == "oracle_rejected"),
            "expected_rejection": float(labels.status != "accepted"),
        },
    )


class _CodingProvider:
    """Only sources and the fixed repair script are supplied, NEVER the independent oracle."""

    configured = True

    def __init__(self, script: CodingScript) -> None:
        self.calls = 0
        self.queue: deque[tuple[str, dict[str, object]]] = deque()
        for path, content in script.initial_files.items():
            self.queue.append(("create_file", {"path": path, "content": content}))
        self.queue.append(("submit_code_ready", {}))
        for path, content in script.repaired_files.items():
            self.queue.append(("read_file", {"path": path}))
            self.queue.append(
                (
                    "edit_file",
                    {
                        "path": path,
                        "old_text": script.initial_files[path],
                        "new_text": content,
                    },
                )
            )
        self.queue.append(("submit_code_ready", {}))

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        self.calls += 1
        if self.calls > 25:
            raise ValueError("Coding fixture callback budget exceeded")
        name, arguments = self.queue.popleft() if self.queue else ("submit_code_ready", {})
        return LLMResult(
            text="",
            model="scripted-coding-provider",
            usage=TokenUsage(0, 0, 0),
            tool_calls=(ToolCall(id=f"fixture-{self.calls}", name=name, arguments=arguments),),
        )

    async def close(self) -> None:
        pass


class InjectedCodingAdapter:
    def __init__(
        self,
        fixture: CodingFixture,
        oracle: CodingOracleFixture,
        *,
        fixture_sha256: str,
        oracle_sha256: str,
        sandbox: SandboxExecutor,
        resources: dict[str, str],
        workspace_root: Path,
    ) -> None:
        if fixture.suite_id != oracle.suite_id:
            raise ValueError("Author and oracle suite IDs differ")
        self.fixture = fixture
        self.oracle = oracle
        self.sandbox = sandbox
        self.resources = resources
        self.workspace_root = workspace_root
        self.oracle_source = (
            Path(__file__).with_name("coding_oracle.py").read_text(encoding="utf-8")
        )
        self.identity = AdapterIdentity(
            name="isolated-coding-agent",
            category="coding",
            variant="baseline",
            execution="injected",
            model="scripted-coding-provider",
            resources={
                **resources,
                "source_fixture": fixture_sha256,
                "oracle_fixture": oracle_sha256,
                "oracle_harness": hashlib.sha256(self.oracle_source.encode()).hexdigest(),
            },
        )

    def validate_case(self, case: BenchmarkCase) -> None:
        query = QueryInputs.model_validate(case.inputs)
        CodingLabels.model_validate(case.expected)
        script = self.fixture.scripts.get(query.query_id)
        oracle = self.oracle.cases.get(query.query_id)
        if (
            script is None
            or oracle is None
            or script.prompt != case.prompt
            or oracle.prompt != case.prompt
        ):
            raise ValueError("Coding author/oracle input binding differs")

    async def execute(self, prompt: str, inputs: dict[str, JsonValue], *, seed: int) -> Observation:
        query = QueryInputs.model_validate(inputs)
        script = self.fixture.scripts[query.query_id]
        oracle = self.oracle.cases[query.query_id]
        if script.prompt != prompt or oracle.prompt != prompt:
            raise ValueError("Coding input changed after preflight")
        provider = _CodingProvider(script)
        sandbox = ObservedSandbox(self.sandbox, self.resources)
        result: OracleResult | None = None
        completed = False
        status: CodingStatus = "agent_failed"
        error: CodingError | None = "code_repair_exhausted"
        async with local_component_task(self.workspace_root, prompt, mode="coding") as (
            task_id,
            store,
            workspace,
        ):
            agent = CodingAgent(
                provider=provider,
                repository=store,
                workspaces=workspace,
                sandbox=sandbox,
                limits=CodingLimits(max_repairs=script.max_repairs, timeout_seconds=30),
            )
            try:
                await agent.run(
                    task_id=task_id, objective=prompt, plan=["Author and validate public function"]
                )
                completed = True
            except CodingRunError as exc:
                if exc.code != "code_repair_exhausted":
                    raise
            validations = [run for request, run in sandbox.runs if request.action == "static_check"]
            tests = [run for request, run in sandbox.runs if request.action == "run_pytest"]
            compile_passed = bool(
                validations
                and validations[-1].status == "succeeded"
                and validations[-1].exit_code == 0
            )
            tests_passed = bool(
                tests and tests[-1].status == "succeeded" and tests[-1].exit_code == 0
            )
            if completed:
                snapshot = workspace.source_snapshot(task_id)
                checked = await sandbox.run(
                    SandboxRunRequest(
                        task_id=task_id,
                        action="run_python",
                        path="eval_oracle.py",
                        timeout_seconds=30,
                        files={
                            "solution.py": snapshot["solution.py"],
                            "eval_oracle.py": self.oracle_source,
                            "oracle_cases.json": json.dumps(oracle.model_dump(), allow_nan=False),
                        },
                        collect_artifacts=["outputs/eval_oracle.json"],
                    )
                )
                result = OracleResult.model_validate(oracle_json(checked))
                if result.total_count != len(oracle.checks):
                    raise ValueError("Oracle did not run all held-out checks")
                status = (
                    "accepted" if result.passed_count == result.total_count else "oracle_rejected"
                )
                error = None if status == "accepted" else "oracle_mismatch"
            actual = CodingObservation(
                status=status,
                error_code=error,
                compile_passed=compile_passed,
                tests_passed=tests_passed,
                agent_completed=completed,
                repairs=max(0, len(validations) - 1),
                oracle=result,
                sandbox_runs=len(sandbox.runs),
            )
            return Observation(
                payload=actual.model_dump(mode="json"),
                usage=fixture_usage(model_calls=provider.calls),
            )

    def score(self, observation: Observation, expected: dict[str, JsonValue]) -> ScoreCard:
        return score_coding(observation, expected)
