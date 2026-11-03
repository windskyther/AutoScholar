"""Trusted held-out checker. Read as text by the adapter; execute ONLY in Docker."""

import json
import math
from pathlib import Path

import solution  # type: ignore[import-not-found]


def equivalent(actual: object, expected: object) -> bool:
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        return (
            isinstance(actual, (int, float))
            and not isinstance(actual, bool)
            and math.isfinite(actual)
            and math.isclose(actual, expected, rel_tol=0, abs_tol=1e-9)
        )
    return type(actual) is type(expected) and actual == expected


def main() -> None:
    oracle = json.loads(Path("oracle_cases.json").read_text(encoding="utf-8"))
    target = getattr(solution, oracle["function"])
    passed = 0
    for check in oracle["checks"]:
        try:
            actual = target(*check["args"], **check.get("kwargs", {}))
        except Exception as error:
            passed += int(type(error).__name__ == check.get("raises"))
        else:
            passed += int(check.get("raises") is None and equivalent(actual, check.get("expected")))
    Path("outputs").mkdir(exist_ok=True)
    Path("outputs/eval_oracle.json").write_text(
        json.dumps(
            {"schema_version": 1, "passed_count": passed, "total_count": len(oracle["checks"])}
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
