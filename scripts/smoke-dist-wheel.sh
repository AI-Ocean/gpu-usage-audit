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
import re
import sys

def canonicalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()

def requirement_name(requirement):
    match = re.match(r"\s*([A-Za-z0-9_.-]+)", requirement)
    return canonicalize(match.group(1)) if match else ""

expected_version = sys.argv[1]
dist = importlib.metadata.distribution("gpu-usage-audit")
if dist.version != expected_version:
    raise SystemExit(f"metadata version {dist.version} != wheel version {expected_version}")
requires = dist.requires or []
if not any(requirement_name(req) == "nvidia-ml-py" for req in requires):
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

mkdir "$tmpdir/fake-pynvml"
cat >"$tmpdir/fake-pynvml/pynvml.py" <<'PY'
NVML_ERROR_DRIVER_NOT_LOADED = 9
NVML_ERROR_LIBRARY_NOT_FOUND = 12
NVML_ERROR_LIB_RM_VERSION_MISMATCH = 19


class NVMLError(Exception):
    def __init__(self, value):
        super().__init__("localized NVML error")
        self.value = value


def nvmlInit():
    raise NVMLError(NVML_ERROR_LIB_RM_VERSION_MISMATCH)
PY

fake_nvml_json="$tmpdir/fake-nvml-doctor.json"
fake_nvml_text="$tmpdir/fake-nvml-doctor.txt"
PYTHONPATH="$tmpdir/fake-pynvml" "$tmpdir/venv/bin/gua" doctor >"$fake_nvml_text"
grep -q "Install or repair the NVIDIA driver" "$fake_nvml_text" || {
  printf 'fake NVML text output is missing driver repair fix\n' >&2
  exit 1
}

PYTHONPATH="$tmpdir/fake-pynvml" "$tmpdir/venv/bin/gua" doctor --json >"$fake_nvml_json"
"$tmpdir/venv/bin/python" - "$fake_nvml_json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    data = json.load(f)
nvml = next(check for check in data["checks"] if check["id"] == "nvml")
if nvml["details"]["loadable"] is not True:
    raise SystemExit("fake pynvml was not loaded")
if nvml["details"]["initialized"] is not False:
    raise SystemExit("fake pynvml init failure was not reported")
summary = nvml["summary"]
if "versions do not match" not in summary:
    raise SystemExit(f"unexpected NVML init summary: {summary}")
if "NVML initialization failed" in summary:
    raise SystemExit(f"summary still has duplicate init prefix: {summary}")
PY

"$tmpdir/venv/bin/gpu-usage-audit" demo --ticks 1 --interval 1ms >/dev/null
