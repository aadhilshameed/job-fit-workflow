"""Build lambda.zip: the code, data/background.txt and every library, compiled for Lambda's Linux.

    python -m scripts.build_lambda

Lambda runs Linux, and this laptop runs Windows. Some libraries (pydantic, psycopg) contain compiled code, so
pip is told to download the Linux versions (--platform) instead of the ones for this machine. Nothing is
installed on the laptop itself; everything goes into build/ and then into lambda.zip.
Upload lambda.zip in the AWS console (Lambda > your function > Code > Upload from > .zip file).
"""

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
ZIP = ROOT / "lambda.zip"
PYTHON_VERSION = "3.14"                # must match the Lambda runtime you pick (Python 3.14)
PLATFORM = "manylinux2014_x86_64"       # Lambda's Linux on x86_64 (the default architecture)


def main():
    shutil.rmtree(BUILD, ignore_errors=True)
    ZIP.unlink(missing_ok=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt"),
                    "--platform", PLATFORM, "--implementation", "cp", "--python-version", PYTHON_VERSION,
                    "--only-binary=:all:", "--target", str(BUILD), "--quiet"], check=True)
    for folder in ("job_fit", "scripts"):
        shutil.copytree(ROOT / folder, BUILD / folder, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(ROOT / "lambda_function.py", BUILD)
    (BUILD / "data").mkdir()
    shutil.copy(ROOT / "data" / "background.txt", BUILD / "data")
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as zf:      # forward slashes inside, as Linux expects
        for path in sorted(BUILD.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                zf.write(path, path.relative_to(BUILD).as_posix())
    print(f"Built {ZIP.name}: {ZIP.stat().st_size / 1_000_000:.1f} MB (Lambda allows 50 MB to upload directly)")


if __name__ == "__main__":
    main()
