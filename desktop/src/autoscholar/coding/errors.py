import re
from dataclasses import dataclass

from autoscholar.coding.sandbox import SandboxRunResult


@dataclass(frozen=True, slots=True)
class ErrorDiagnostic:
    category: str
    summary: str
    stdout: str
    stderr: str


class ErrorParser:
    _categories = (
        ("syntax_error", ("syntaxerror", "indentationerror")),
        ("import_error", ("importerror", "modulenotfounderror")),
        ("cuda_oom", ("cuda out of memory", "cudnn_status_alloc_failed")),
        ("memory_error", ("memoryerror", "cannot allocate memory", "out of memory")),
        ("shape_error", ("shape", "size mismatch", "mat1 and mat2")),
        ("file_not_found", ("filenotfounderror", "no such file or directory")),
        ("nan_or_divergence", ("loss is nan", "nan detected", "diverged")),
        ("assertion_error", ("assertionerror", "failed")),
    )
    _secret_patterns = (
        re.compile(r"(?i)(api[_-]?key|token|secret|password)(\s*[=:]\s*)\S+"),
        re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    )

    @classmethod
    def parse(cls, result: SandboxRunResult) -> ErrorDiagnostic:
        stdout = cls._redact(result.stdout)
        stderr = cls._redact(result.stderr)
        combined = f"{stderr}\n{stdout}".casefold()
        if result.status == "timed_out":
            category = "timeout"
        else:
            category = "runtime_error"
            for candidate, markers in cls._categories:
                if any(marker in combined for marker in markers):
                    category = candidate
                    break
        useful = stderr.strip() or stdout.strip() or "Execution failed without output"
        return ErrorDiagnostic(
            category=category,
            summary=useful[-4_000:],
            stdout=stdout[-8_000:],
            stderr=stderr[-8_000:],
        )

    @classmethod
    def _redact(cls, value: str) -> str:
        redacted = value
        for pattern in cls._secret_patterns:
            if pattern.groups:
                redacted = pattern.sub(r"\1\2[REDACTED]", redacted)
            else:
                redacted = pattern.sub("[REDACTED]", redacted)
        return redacted
