import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts import build_lambda_package


@pytest.mark.parametrize(("job", "requirements"), [
    ("bist", "bist.txt"), ("nasdaq", "nasdaq.txt"), ("crypto", "binance.txt"),
    ("fx", "fx.txt"), ("tefas", "tefas.txt"),
])
def test_build_includes_code_and_correct_dependencies(monkeypatch, tmp_path, job, requirements):
    calls = []

    def fake_install(command, check):
        calls.append(command)
        target = Path(command[command.index("--target") + 1])
        (target / "dependency.py").write_text("# fake package\n")

    monkeypatch.setattr(build_lambda_package.subprocess, "run", fake_install)
    output = tmp_path / "output" / "lambda.zip"
    build_lambda_package.build_package(job, output)
    assert calls[0][calls[0].index("--requirement") + 1].endswith("/requirements/jobs/" + requirements)
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert "app/lambda_handlers/common.py" in names
        assert "app/lambda_handlers/crypto_prices.py" in names
        assert "dependency.py" in names
        assert not any("__pycache__" in name or "/tests/" in name for name in names)


@pytest.mark.parametrize("job", ["bist", "nasdaq", "crypto", "fx", "tefas"])
def test_job_specific_script_still_supports_output_option(job):
    script = Path(__file__).resolve().parents[1] / "scripts" / f"build_{job}_lambda_package.py"
    result = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True, check=True)
    assert "--output" in result.stdout
