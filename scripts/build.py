#!/usr/bin/env python3
"""Create a standalone, dependency-free Python zip application."""
import hashlib
from pathlib import Path
import shutil
import tempfile
import zipapp

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    work = ROOT / ".work"
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="package-", dir=work) as tmp:
        stage = Path(tmp)
        shutil.copytree(ROOT / "src/zeus_code", stage / "zeus_code",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (stage / "__main__.py").write_text("from zeus_code.cli import main\nraise SystemExit(main())\n")
        artifact = output / "zeus-code.pyz"
        zipapp.create_archive(stage, artifact, interpreter="/usr/bin/env python3",
                              compressed=True)
        artifact.chmod(0o755)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (output / "SHA256SUMS").write_text(f"{digest}  {artifact.name}\n")
    print(f"Built {artifact.relative_to(ROOT)} ({artifact.stat().st_size:,} bytes)")
    print(f"SHA256 {digest}")


if __name__ == "__main__":
    main()
