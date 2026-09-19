import secrets

from fastapi import Request

from autoscholar.core.errors import AppError


def require_experiment_token(request: Request) -> None:
    configured = request.app.state.settings.experiment_api_token
    expected = configured.get_secret_value() if configured else ""
    if not expected:
        raise AppError(
            status_code=503,
            code="experiment_api_not_configured",
            message="Experiment API access is not configured",
        )
    authorization = request.headers.get("authorization", "")
    scheme, separator, supplied = authorization.partition(" ")
    if (
        not separator
        or scheme.casefold() != "bearer"
        or not secrets.compare_digest(supplied, expected)
    ):
        raise AppError(
            status_code=401,
            code="experiment_auth_required",
            message="A valid experiment access token is required",
        )
