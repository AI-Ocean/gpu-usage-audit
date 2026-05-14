#!/usr/bin/env bash
set -euo pipefail

wheel="${1:-}"
if [[ -z "$wheel" ]]; then
  shopt -s nullglob
  wheels=(dist/*.whl)
  if [[ "${#wheels[@]}" -ne 1 ]]; then
    printf 'expected exactly one wheel in dist/, found %s\n' "${#wheels[@]}" >&2
    exit 1
  fi
  wheel="${wheels[0]}"
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

python_bin="${PYTHON:-}"
if [[ -z "$python_bin" ]]; then
  if command -v uv >/dev/null 2>&1; then
    python_bin="$(uv python find)"
  elif command -v python3 >/dev/null 2>&1; then
    python_bin="python3"
  else
    python_bin="python"
  fi
fi

"$python_bin" -m venv "$tmpdir/venv"
"$tmpdir/venv/bin/python" -m pip install "$wheel"

"$tmpdir/venv/bin/gpu-usage-audit" version
"$tmpdir/venv/bin/gua" --version
"$tmpdir/venv/bin/gua" doctor
"$tmpdir/venv/bin/gua" start --dry-run
