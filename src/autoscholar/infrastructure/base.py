from typing import Protocol


class ManagedDependency(Protocol):
    async def ping(self) -> bool: ...

    async def close(self) -> None: ...

