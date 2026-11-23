import subprocess
import sys


def run(command: list[str]) -> int:
    return subprocess.run(command, check=False).returncode


raise SystemExit(
    run([sys.executable, "-m", "compileall", "-q", "."])
    or run(["ruff", "check", "."])
)
