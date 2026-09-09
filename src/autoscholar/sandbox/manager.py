import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from autoscholar.coding.sandbox import (
    DockerLimits,
    DockerSandboxExecutor,
    SandboxError,
    SandboxExecutor,
    SandboxHealth,
    SandboxRunRequest,
    SandboxRunResult,
)
from autoscholar.core.responses import UTF8JSONResponse


def create_manager_app(executor: SandboxExecutor | None = None) -> FastAPI:
    resolved = executor or DockerSandboxExecutor(
        image=os.getenv("SANDBOX_IMAGE", "autoscholar-python-sandbox:phase4"),
        dataset_volume=os.getenv("SANDBOX_DATASET_VOLUME", "autoscholar_mnist_data"),
        dataset_ready_file=os.getenv(
            "SANDBOX_DATASET_READY_FILE", "/datasets/mnist/.autoscholar-ready"
        ),
        limits=DockerLimits(
            memory_bytes=int(os.getenv("SANDBOX_MEMORY_BYTES", str(4 * 1024**3))),
            nano_cpus=int(os.getenv("SANDBOX_NANO_CPUS", "2000000000")),
            pids_limit=int(os.getenv("SANDBOX_PIDS_LIMIT", "256")),
            max_output_bytes=int(os.getenv("SANDBOX_MAX_OUTPUT_BYTES", "65536")),
        ),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await resolved.close()

    app = FastAPI(
        title="AutoScholar Sandbox Manager",
        default_response_class=UTF8JSONResponse,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.exception_handler(SandboxError)
    async def sandbox_error(_: object, exc: SandboxError) -> UTF8JSONResponse:
        return UTF8JSONResponse(
            status_code=503,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.get("/internal/health", response_model=SandboxHealth)
    async def health() -> SandboxHealth:
        return await resolved.health()

    @app.post("/internal/v1/run", response_model=SandboxRunResult)
    async def run(payload: SandboxRunRequest) -> SandboxRunResult:
        return await resolved.run(payload)

    return app


app = create_manager_app()
