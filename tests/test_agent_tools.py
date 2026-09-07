import math

from autoscholar.agent.tools import CalculatorTool, RestrictedPythonTool


async def test_calculator_evaluates_allowed_math() -> None:
    result = await CalculatorTool().execute({"expression": "sqrt(81) + sin(pi / 2)"})

    assert result.succeeded is True
    assert math.isclose(float(result.output), 10.0)


async def test_calculator_accepts_caret_as_mathematical_exponent() -> None:
    result = await CalculatorTool().execute({"expression": "2^3"})

    assert result.succeeded is True
    assert result.output == "8"


async def test_calculator_rejects_unsafe_syntax() -> None:
    result = await CalculatorTool().execute({"expression": "__import__('os').getcwd()"})

    assert result.succeeded is False
    assert result.error_code == "calculator_invalid_expression"


async def test_restricted_python_runs_a_deterministic_calculation() -> None:
    result = await RestrictedPythonTool().execute(
        {"code": "values = [x * x for x in range(11)]\nprint(min(values), max(values))"}
    )

    assert result.succeeded is True
    assert result.output == "0 100"


async def test_restricted_python_rejects_imports_and_attributes() -> None:
    tool = RestrictedPythonTool()

    imported = await tool.execute({"code": "import os\nprint(os.getcwd())"})
    attribute = await tool.execute({"code": "print((1).__class__)"})

    assert imported.error_code == "python_validation_failed"
    assert attribute.error_code == "python_validation_failed"


async def test_restricted_python_enforces_timeout() -> None:
    result = await RestrictedPythonTool(timeout_seconds=0.01).execute(
        {"code": "total = 0\nfor x in range(1000000000):\n    total += x\nprint(total)"}
    )

    assert result.succeeded is False
    assert result.error_code == "python_timeout"


async def test_restricted_python_truncates_output() -> None:
    result = await RestrictedPythonTool(max_output_chars=20).execute({"code": "print('x' * 100)"})

    assert result.succeeded is True
    assert result.output.endswith("...[output truncated]")
