"""GitHub Release 태그와 pyproject 버전이 같은지 확인."""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Any


def main() -> int:
    ref_name = os.environ.get("GITHUB_REF_NAME")
    if ref_name is None:
        print("GITHUB_REF_NAME is required", file=sys.stderr)
        return 2

    with Path("pyproject.toml").open("rb") as f:
        pyproject: dict[str, Any] = tomllib.load(f)

    version = pyproject["project"]["version"]
    tag_version = ref_name.removeprefix("v")
    if version != tag_version:
        print(f"pyproject version {version} != tag {tag_version}", file=sys.stderr)
        return 1

    print(f"tag version OK: {ref_name} matches pyproject {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
