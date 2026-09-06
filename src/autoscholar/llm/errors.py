from autoscholar.core.errors import AppError


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


class LLMUnavailableError(AppError):
    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(status_code=503, code=code, message=message)

