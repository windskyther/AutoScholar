from autoscholar.core.errors import AppError
from autoscholar.llm.models import TokenUsage


class LLMNotConfiguredError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status_code=503,
            code="llm_not_configured",
            message="The language model provider is not configured",
        )


class LLMUpstreamError(AppError):
    def __init__(self, *, code: str = "llm_upstream_error", message: str) -> None:
        super().__init__(status_code=502, code=code, message=message)


class LLMResponseError(LLMUpstreamError):
    """A received but unusable completion still incurs measurable token usage."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        usage: TokenUsage | None,
        retryable: bool = True,
        model: str | None = None,
        finish_reason: str | None = None,
        validation_reason: str | None = None,
    ) -> None:
        super().__init__(code=code, message=message)
        self.usage = usage
        self.retryable = retryable
        self.model = model
        self.finish_reason = finish_reason
        self.validation_reason = validation_reason


class LLMUnavailableError(AppError):
    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(status_code=503, code=code, message=message)
