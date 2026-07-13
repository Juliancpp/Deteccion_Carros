#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = "/" + "/".join(["home", "julian", "repos", "relax_julian"])
CHECK_SUFFIXES = {".py", ".sh", ".service", ".timer", ".js", ".html", ".css", ".example"}
SKIP_DIRS = {".git", "venv", ".venv", "__pycache__", "data", "database", "dist", "runs"}


def iter_versioned_like_files() -> list[Path]:
    files: list[Path] = []
    for root, dirs, names in os.walk(PROJECT_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            path = Path(root) / name
            if path.suffix in CHECK_SUFFIXES or path.name in {".env.example", ".gitignore"}:
                files.append(path)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail if executable/versioned code contains development-machine absolute paths.")
    parser.add_argument("--report-env", action="store_true", help="Report .env variable names whose values contain absolute local paths.")
    args = parser.parse_args()
    failures = []
    needle = FORBIDDEN.lower()
    for path in iter_versioned_like_files():
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        if needle in text:
            failures.append(str(path.relative_to(PROJECT_ROOT)))
    if args.report_env:
        env_path = PROJECT_ROOT / ".env"
        if env_path.exists():
            names = []
            for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    if needle in value.lower():
                        names.append(key.strip())
            if names:
                print("Variables .env con rutas absolutas locales: " + ", ".join(sorted(names)))
    if failures:
        print("Rutas no portables encontradas:")
        for item in failures:
            print(f"- {item}")
        return 1
    print("OK: no hay dependencias rígidas hacia rutas locales de desarrollo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
