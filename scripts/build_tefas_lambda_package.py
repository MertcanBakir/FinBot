"""Build a Python 3.12 x86_64 Lambda ZIP for Amazon Linux 2023."""

import argparse
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("finbot-tefas-lambda.zip"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    output = args.output.resolve()

    with tempfile.TemporaryDirectory(prefix="finbot-tefas-lambda-") as temporary:
        target = Path(temporary)
        subprocess.run(
            [
                sys.executable, "-m", "pip", "install",
                "--requirement", str(root / "requirements/jobs/tefas.txt"),
                "--target", str(target),
                "--platform", "manylinux_2_28_x86_64",
                "--platform", "manylinux_2_27_x86_64",
                "--platform", "manylinux2014_x86_64",
                "--implementation", "cp",
                "--python-version", "3.12",
                "--only-binary=:all:",
                "--no-compile",
            ],
            check=True,
        )
        shutil.copytree(
            root / "app", target / "app",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(target.rglob("*")):
                relative = file.relative_to(target)
                if file.is_file() and not {"__pycache__", "tests"}.intersection(relative.parts):
                    archive.write(file, relative)
    print(f"Lambda deployment package created: {output}")


if __name__ == "__main__":
    main()
