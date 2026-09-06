from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from autoscholar import __version__

router = APIRouter(prefix="/health", tags=["health"])


class LiveResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: Literal["autoscholar"] = "autoscholar"
    version: str


@router.get("/live", response_model=LiveResponse)
async def live() -> LiveResponse:
    return LiveResponse(version=__version__)
