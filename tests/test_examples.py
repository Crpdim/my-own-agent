import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_query_example_runs_outside_the_checkout_without_cli_or_credentials(tmp_path):
    example = tmp_path / "embedded_query.py"
    shutil.copy2(Path("examples/embedded_query.py"), example)
    # Isolated mode ignores checkout paths; the installed package supplies the runtime.
    result = subprocess.run(
        [sys.executable, "-I", str(example)],
        cwd=tmp_path,
        env={
            key: value
            for key, value in os.environ.items()
            if key not in {"OPENAI_API_KEY", "DEEPSEEK_API_KEY"}
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "DEMO-001: shipped (offline fixture)"
    assert result.stderr == ""
