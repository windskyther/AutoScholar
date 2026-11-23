import os
import subprocess
import sys
from pathlib import Path


def test_collection_bootstrap_cannot_load_local_or_inherited_supplier_credentials(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "LLM_API_KEY=synthetic-file-only\nLLM_MODEL=synthetic-model\n", encoding="utf-8"
    )
    root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "LLM_API_KEY": "synthetic-inherited-only",
        "LLM_MODEL": "synthetic-model",
        "PYTHONPATH": os.pathsep.join((str(root), str(root / "src"))),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import tests.conftest; from autoscholar.main import app; "
            "assert not app.state.llm_provider.configured; "
            "assert app.state.settings.llm_api_key is None; print('isolated')",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "isolated"
