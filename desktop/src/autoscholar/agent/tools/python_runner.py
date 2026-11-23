import ast
import asyncio
import os
import sys
import tempfile
import time
from typing import Any, ClassVar

from autoscholar.agent.tools.base import ToolExecutionResult
from autoscholar.llm import ToolDefinition


class PythonValidationError(ValueError):
    pass


class RestrictedPythonTool:
    """A constrained subprocess runner; this is not an OS-level security sandbox."""

    _allowed_nodes = (
        ast.Module,
        ast.Expr,
        ast.Assign,
        ast.AnnAssign,
        ast.AugAssign,
        ast.For,
        ast.If,
        ast.Pass,
        ast.Break,
        ast.Continue,
        ast.Name,
        ast.Load,
        ast.Store,
        ast.Constant,
        ast.List,
        ast.Tuple,
        ast.Dict,
        ast.Set,
        ast.Subscript,
        ast.Slice,
        ast.BinOp,
        ast.UnaryOp,
        ast.BoolOp,
        ast.Compare,
        ast.IfExp,
        ast.Call,
        ast.keyword,
        ast.ListComp,
        ast.SetComp,
        ast.DictComp,
        ast.GeneratorExp,
        ast.comprehension,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.FloorDiv,
        ast.Mod,
        ast.Pow,
        ast.MatMult,
        ast.UAdd,
        ast.USub,
        ast.Not,
        ast.And,
        ast.Or,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.In,
        ast.NotIn,
        ast.Is,
        ast.IsNot,
    )
    _allowed_calls: ClassVar[set[str]] = {
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "enumerate",
        "float",
        "int",
        "len",
        "list",
        "max",
        "min",
        "print",
        "range",
        "reversed",
        "round",
        "set",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
    }

    def __init__(
        self,
        *,
        timeout_seconds: float = 3.0,
        max_code_chars: int = 4_000,
        max_output_chars: int = 16_000,
    ) -> None:
        self._timeout = timeout_seconds
        self._max_code_chars = max_code_chars
        self._max_output_chars = max_output_chars

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="python",
            description=(
                "Run short deterministic Python calculations. Imports, files, network, process "
                "access, attributes, functions/classes, exceptions, and while loops are blocked. "
                "Print the values needed for the answer."
            ),
            parameters={
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
                "additionalProperties": False,
            },
        )

    async def execute(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        started = time.perf_counter()
        code = arguments.get("code")
        try:
            if not isinstance(code, str) or not code.strip():
                raise PythonValidationError("code must be a non-empty string")
            self._validate(code)
        except (PythonValidationError, SyntaxError) as exc:
            return self._failure(started, "python_validation_failed", str(exc))

        with tempfile.TemporaryDirectory(prefix="autoscholar-python-") as working_directory:
            environment = {"PYTHONIOENCODING": "utf-8"}
            if os.name == "nt" and "SYSTEMROOT" in os.environ:
                environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-S",
                "-c",
                code,
                cwd=working_directory,
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=self._timeout
                )
            except TimeoutError:
                process.kill()
                await process.wait()
                return self._failure(
                    started,
                    "python_timeout",
                    f"Execution exceeded {self._timeout:g} seconds",
                )

        output = (stdout if process.returncode == 0 else stderr).decode("utf-8", errors="replace")
        output = output.strip() or "(no output)"
        if len(output) > self._max_output_chars:
            output = output[: self._max_output_chars] + "\n...[output truncated]"
        if process.returncode != 0:
            return self._failure(started, "python_execution_failed", output)
        return ToolExecutionResult(
            output=output,
            succeeded=True,
            duration_ms=self._duration(started),
        )

    def _validate(self, code: str) -> None:
        if len(code) > self._max_code_chars:
            raise PythonValidationError(f"code exceeds {self._max_code_chars} characters")
        tree = ast.parse(code, mode="exec")
        nodes = list(ast.walk(tree))
        if len(nodes) > 500:
            raise PythonValidationError("code is too complex")
        for node in nodes:
            if not isinstance(node, self._allowed_nodes):
                raise PythonValidationError(f"{type(node).__name__} is not allowed")
            if isinstance(node, ast.Name) and node.id.startswith("_"):
                raise PythonValidationError("private names are not allowed")
            if isinstance(node, ast.Call) and (
                not isinstance(node.func, ast.Name) or node.func.id not in self._allowed_calls
            ):
                raise PythonValidationError("function call is not allowed")

    def _failure(self, started: float, code: str, output: str) -> ToolExecutionResult:
        return ToolExecutionResult(
            output=output[: self._max_output_chars],
            succeeded=False,
            duration_ms=self._duration(started),
            error_code=code,
        )

    @staticmethod
    def _duration(started: float) -> float:
        return round((time.perf_counter() - started) * 1_000, 3)
