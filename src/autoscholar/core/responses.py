from fastapi.responses import JSONResponse


class UTF8JSONResponse(JSONResponse):
    """JSON response with an explicit charset for legacy Windows clients."""

    media_type = "application/json; charset=utf-8"
