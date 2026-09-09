import subprocess
import sys
import time
from pathlib import Path

marker = Path("/workspace/.autoscholar-source-ready")
deadline = time.monotonic() + 15
while not marker.is_file():
    if time.monotonic() >= deadline:
        print("source snapshot was not provided", file=sys.stderr)
        raise SystemExit(124)
    time.sleep(0.05)

if len(sys.argv) < 2:
    print("sandbox command was not provided", file=sys.stderr)
    raise SystemExit(2)

raise SystemExit(subprocess.run(sys.argv[1:], check=False).returncode)
