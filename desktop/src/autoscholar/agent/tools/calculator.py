import ast
import math
import operator
import time
from collections.abc import Callable
from typing import Any, ClassVar

from autoscholar.agent.tools.base import ToolExecutionResult
from autoscholar.llm import ToolDefinition

Number = int | float


class CalculatorError(ValueError):
    pass


class CalculatorTool:
    _binary: ClassVar[dict[type[ast.operator], Callable[[Number, Number], Number]]] = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
        ast.BitXor: operator.pow,
    }
    _unary: ClassVar[dict[type[ast.unaryop], Callable[[Number], Number]]] = {
        ast.UAdd: operator.pos,
        ast.USub: operator.neg,
    }
    _functions: ClassVar[dict[str, Callable[..., Number]]] = {
        "abs": abs,
        "ceil": math.ceil,
        "cos": math.cos,
        "exp": math.exp,
        "floor": math.floor,
        "log": math.log,
        "log10": math.log10,
        "max": max,
        "min": min,
        "round": round,
        "sin": math.sin,
        "sqrt": math.sqrt,
        "tan": math.tan,
    }
    _constants: ClassVar[dict[str, Number]] = {
        "e": math.e,
        "pi": math.pi,
        "tau": math.tau,
    }

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="calculator",
            description=(
                "Safely evaluate a single arithmetic expression. Supports standard arithmetic, "
                "pi/e/tau, and common functions such as sqrt, log, sin, cos, min, and max."
            ),
            parameters={
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
                "additionalProperties": False,
            },
        )

    async def execute(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        started = time.perf_counter()
        expression = arguments.get("expression")
        try:
            if not isinstance(expression, str) or not expression.strip():
                raise CalculatorError("expression must be a non-empty string")
            if len(expression) > 1_000:
                raise CalculatorError("expression exceeds 1000 characters")
            tree = ast.parse(expression, mode="eval")
            if sum(1 for _ in ast.walk(tree)) > 100:
                raise CalculatorError("expression is too complex")
            value = self._evaluate(tree.body)
            if isinstance(value, float) and not math.isfinite(value):
                raise CalculatorError("result is not finite")
            output = repr(value)
            return ToolExecutionResult(
                output=output,
                succeeded=True,
                duration_ms=self._duration(started),
            )
        except (CalculatorError, SyntaxError, ArithmeticError, ValueError, TypeError) as exc:
            return ToolExecutionResult(
                output=str(exc),
                succeeded=False,
                duration_ms=self._duration(started),
                error_code="calculator_invalid_expression",
            )

    def _evaluate(self, node: ast.expr) -> Number:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise CalculatorError("only numeric constants are allowed")
            return node.value
        if isinstance(node, ast.Name) and node.id in self._constants:
            return self._constants[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in self._binary:
            left = self._evaluate(node.left)
            right = self._evaluate(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 1_000:
                raise CalculatorError("exponent is too large")
            result = self._binary[type(node.op)](left, right)
            if abs(result) > 1e100:
                raise CalculatorError("result magnitude is too large")
            return result
        if isinstance(node, ast.UnaryOp) and type(node.op) in self._unary:
            return self._unary[type(node.op)](self._evaluate(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            function = self._functions.get(node.func.id)
            if function is None or node.keywords:
                raise CalculatorError("function is not allowed")
            return function(*(self._evaluate(argument) for argument in node.args))
        raise CalculatorError(f"unsupported expression element: {type(node).__name__}")

    @staticmethod
    def _duration(started: float) -> float:
        return round((time.perf_counter() - started) * 1_000, 3)
