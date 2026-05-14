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
wheel_name="$(basename "$wheel")"
expected_version="${wheel_name#gpu_usage_audit-}"
expected_version="${expected_version%%-*}"

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
"$tmpdir/venv/bin/python" -m pip install --no-deps --no-index "$wheel"

"$tmpdir/venv/bin/python" - "$expected_version" <<'PY'
import importlib.metadata
import sys

expected_version = sys.argv[1]
dist = importlib.metadata.distribution("gpu-usage-audit")
if dist.version != expected_version:
    raise SystemExit(f"metadata version {dist.version} != wheel version {expected_version}")
requires = dist.requires or []
if not any(req.split(";")[0].strip().lower().startswith("nvidia-ml-py") for req in requires):
    raise SystemExit("wheel metadata does not require nvidia-ml-py")
PY

actual_version="$("$tmpdir/venv/bin/gpu-usage-audit" version)"
if [[ "$actual_version" != "$expected_version" ]]; then
  printf 'gpu-usage-audit version %s != wheel version %s\n' \
    "$actual_version" "$expected_version" >&2
  exit 1
fi

gua_version="$("$tmpdir/venv/bin/gua" --version)"
if [[ "$gua_version" != "$expected_version" ]]; then
  printf 'gua version %s != wheel version %s\n' "$gua_version" "$expected_version" >&2
  exit 1
fi

"$tmpdir/venv/bin/gua" doctor
"$tmpdir/venv/bin/gua" doctor --json >/dev/null
"$tmpdir/venv/bin/gpu-usage-audit" demo --ticks 1 --interval 1ms >/dev/null
