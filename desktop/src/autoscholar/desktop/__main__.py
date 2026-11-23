import asyncio
import ctypes
import re
import socket
import sys
from pathlib import Path

import uvicorn
from pydantic import SecretStr

from autoscholar.desktop.preferences import library_folder
from autoscholar.desktop.runtime import desktop_app
from autoscholar.desktop.secrets import master_key


async def serve() -> None:
    if sys.platform != "win32":
        raise OSError("Desktop launch requires Windows")
    preferences, assets = map(Path, sys.argv[1:])
    folder = library_folder(preferences)
    folder.mkdir(parents=True, exist_ok=True)
    token = sys.stdin.readline().strip()
    if not re.fullmatch("[a-f0-9]{64}", token):
        raise RuntimeError("Desktop session handshake failed")
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        app = desktop_app(
            folder,
            assets,
            assets.parent / "qdrant.exe",
            SecretStr(master_key(preferences)),
            token,
            port,
            idle=lambda: print("IDLE", flush=True),
        )
        app.state.preferences = preferences
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, proxy_headers=False))

        # A blocking read on a Windows anonymous pipe can stall interpreter startup
        # in a spawned model worker that inherits the same standard input handle.
        import msvcrt

        pipe = ctypes.c_void_p(msvcrt.get_osfhandle(sys.stdin.fileno()))
        peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe

        async def watch_parent() -> None:
            while peek(pipe, None, 0, None, None, None):
                await asyncio.sleep(0.5)
            server.should_exit = True

        monitor = asyncio.create_task(watch_parent())
        running = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started and not running.done():
            await asyncio.sleep(0.05)
        if server.started:
            print(f"READY {port}", flush=True)
        try:
            await running
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(serve())
