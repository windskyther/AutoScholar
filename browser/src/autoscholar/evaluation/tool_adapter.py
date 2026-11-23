"""Predetermined tool selections against the real, local Calculator implementation."""

import ast
import math
from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from autoscholar.agent.tools import CalculatorTool
from autoscholar.evaluation.component_fixture import QueryInputs, VersionedFixture, fixture_usage
from autoscholar.evaluation.datasets import payload_digest
from autoscholar.evaluation.models import (
    AdapterIdentity,
    BenchmarkCase,
    EvaluationModel,
    FiniteNumber,
    Identifier,
    Observation,
    ScoreCard,
)
from autoscholar.tool_platform.gateway import ToolGatewayError, validate

ToolError = Literal["unknown_tool", "invalid_arguments", "calculator_invalid_expression"]


class ToolCallFixture(EvaluationModel):
    prompt: str = Field(min_length=1, max_length=10000)
    tool: Identifier
    arguments: dict[str, JsonValue] = Field(max_length=10)


class ToolFixture(VersionedFixture):
    calls: dict[Identifier, ToolCallFixture] = Field(min_length=1, max_length=1000)


class ToolLabels(EvaluationModel):
    tool: Identifier
    arguments: dict[str, JsonValue] = Field(max_length=10)
    succeeded: bool
    result: FiniteNumber | None = None
    error_code: ToolError | None = None
    tolerance: Annotated[float, Field(ge=0, le=0.001)] = 1e-9

    @model_validator(mode="after")
    def consistent(self) -> "ToolLabels":
        if self.succeeded != (self.result is not None) or self.succeeded == bool(self.error_code):
            raise ValueError("Success needs a finite result; rejection needs an error code")
        return self


class ToolObservation(EvaluationModel):
    tool: Identifier
    arguments_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    available: bool
    schema_valid: bool
    invoked: bool
    succeeded: bool
    result: FiniteNumber | None = None
    error_code: ToolError | None = None


def _bounded_expression(arguments: dict[str, JsonValue]) -> None:
    """Prevent fixture-driven CPU/memory exhaustion before entering a synchronous tool.

    This is a benchmark input bound, not a claim about arbitrary Calculator safety.
    Both ** and Calculator's ^ alias require a small literal exponent in this harness.
    Invalid syntax is left to the real tool so expected refusals remain measurable.
    """
    expression = arguments.get("expression")
    if not isinstance(expression, str):
        return
    if len(expression) > 1000:
        raise ValueError("Benchmark expression is too long")
    try:
        nodes = list(ast.walk(ast.parse(expression, mode="eval")))
    except SyntaxError:
        return
    if len(nodes) > 100:
        raise ValueError("Benchmark expression is too complex")
    for node in nodes:
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Pow, ast.BitXor)):
            exponent = node.right
            if isinstance(exponent, ast.UnaryOp) and isinstance(exponent.op, (ast.USub, ast.UAdd)):
                exponent = exponent.operand
            if (
                not isinstance(exponent, ast.Constant)
                or isinstance(exponent.value, bool)
                or not isinstance(exponent.value, (int, float))
                or abs(exponent.value) > 1000
                or not math.isfinite(exponent.value)
            ):
                raise ValueError("Benchmark power requires a bounded literal exponent")


def score_tool(observation: Observation, expected: dict[str, JsonValue]) -> ScoreCard:
    labels = ToolLabels.model_validate(expected)
    result = ToolObservation.model_validate(observation.payload)
    selected = result.tool == labels.tool
    arguments = result.arguments_sha256 == payload_digest(labels.arguments)
    numeric = (
        result.succeeded
        and result.result is not None
        and labels.result is not None
        and math.isclose(result.result, labels.result, rel_tol=0, abs_tol=labels.tolerance)
    )
    checks = {
        "tool_selection": selected,
        "arguments": arguments,
        "execution_outcome": result.succeeded == labels.succeeded,
        "error_classification": result.error_code == labels.error_code,
        "result": numeric if labels.succeeded else result.result is None,
    }
    return ScoreCard(
        checks=checks,
        metrics={
            "tool_selection_accuracy": float(selected),
            "argument_accuracy": float(arguments),
            "parameter_schema_validity": float(result.schema_valid),
            "tool_availability": float(result.available),
            "tool_invocations": float(result.invoked),
            "execution_success": float(result.succeeded),
            # Rejections are not numerical answers and never get perfect result accuracy.
            "result_accuracy": float(selected and arguments and numeric)
            if labels.succeeded
            else None,
            "expected_rejection": float(not labels.succeeded),
        },
    )


class FixtureToolAdapter:
    def __init__(self, fixture: ToolFixture, *, fixture_sha256: str) -> None:
        self.fixture = fixture
        self.identity = AdapterIdentity(
            name="local-calculator-fixture",
            category="tool",
            variant="baseline",
            execution="fixture",
            resources={"call_fixture": fixture_sha256},
        )

    def validate_case(self, case: BenchmarkCase) -> None:
        query = QueryInputs.model_validate(case.inputs)
        ToolLabels.model_validate(case.expected)
        call = self.fixture.calls.get(query.query_id)
        if call is None or call.prompt != case.prompt:
            raise ValueError("Tool fixture does not match benchmark prompt")
        payload_digest(
            call.arguments
        )  # Reject non-JSON/non-finite arguments, even in direct injection.
        _bounded_expression(call.arguments)

    async def execute(self, prompt: str, inputs: dict[str, JsonValue], *, seed: int) -> Observation:
        query = QueryInputs.model_validate(inputs)
        call = self.fixture.calls[query.query_id]
        if call.prompt != prompt:
            raise ValueError("Tool query changed after validation")
        _bounded_expression(call.arguments)
        tool = CalculatorTool()
        available = call.tool == tool.definition.name
        schema_valid = False
        invoked = False
        succeeded = False
        number: float | None = None
        error: ToolError | None = "unknown_tool"
        if available:
            try:
                validate(tool.definition.parameters, call.arguments, "invalid_arguments")
            except ToolGatewayError:
                error = "invalid_arguments"
            else:
                schema_valid = True
                invoked = True
                actual = await tool.execute(dict(call.arguments))
                if actual.error_code != (
                    None if actual.succeeded else "calculator_invalid_expression"
                ):
                    raise ValueError("Unexpected Calculator outcome classification")
                succeeded = actual.succeeded
                error = None if succeeded else "calculator_invalid_expression"
                if succeeded:
                    number = float(actual.output)
                    if not math.isfinite(number):
                        raise ValueError("Calculator returned a non-finite value")
        return Observation(
            payload=ToolObservation(
                tool=call.tool,
                arguments_sha256=payload_digest(call.arguments),
                available=available,
                schema_valid=schema_valid,
                invoked=invoked,
                succeeded=succeeded,
                result=number,
                error_code=error,
            ).model_dump(mode="json"),
            usage=fixture_usage(),
        )

    def score(self, observation: Observation, expected: dict[str, JsonValue]) -> ScoreCard:
        return score_tool(observation, expected)
