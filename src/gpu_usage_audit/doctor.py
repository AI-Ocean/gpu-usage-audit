"""`gua doctor` 의 읽기 전용 로컬 bare-metal readiness 진단."""

from __future__ import annotations

import contextlib
import glob
import os
import platform
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .nvml import NVMLNotAvailableError, _decode, _load_pynvml, nvml_init_error_message
from .paths import DEFAULT_DB_PATH, expand_path, is_default_db_path

type CheckStatus = Literal["ok", "warning", "error", "skipped"]
type ReadinessMode = Literal["host", "unsupported"]
type Which = Callable[[str], str | None]

DEFAULT_COMMAND_TIMEOUT_SECONDS = 3.0
COLLECT_COMMAND = "gua daemon --interval 30s"
REPORT_COMMAND = "gua report --since 1h"


@dataclass(slots=True)
class CommandResult:
    """진단 probe 와 테스트에서 쓰는 작은 subprocess 결과 래퍼."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


type CommandRunner = Callable[[Sequence[str], float], CommandResult]


@dataclass(slots=True)
class DoctorCheck:
    """읽기 전용 readiness 진단 항목 하나."""

    id: str
    name: str
    status: CheckStatus
    summary: str
    details: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class OSInfo:
    system: str
    release: str
    platform_string: str
    python_version: str
    python_executable: str
    is_linux: bool


@dataclass(slots=True)
class NvidiaDeviceInfo:
    paths: list[str]
    gpu_device_paths: list[str]
    control_device_paths: list[str]


@dataclass(slots=True)
class NvidiaSMIInfo:
    found: bool
    path: str | None = None
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    gpu_lines: list[str] = field(default_factory=list)
    mig_lines: list[str] = field(default_factory=list)


@dataclass(slots=True)
class NVMLInfo:
    loadable: bool
    initialized: bool
    device_count: int | None = None
    driver_version: str | None = None
    error: str | None = None


type NVMLProbe = Callable[[], NVMLInfo]


@dataclass(slots=True)
class DatabaseInfo:
    path: str
    is_default: bool
    exists: bool
    is_file: bool
    parent_exists: bool
    parent_writable: bool
    size_bytes: int | None = None
    error: str | None = None


@dataclass(slots=True)
class DetectionFacts:
    os: OSInfo
    devices: NvidiaDeviceInfo
    nvidia_smi: NvidiaSMIInfo
    nvml: NVMLInfo
    database: DatabaseInfo


@dataclass(slots=True)
class DoctorPlan:
    """`gua doctor` 의 로컬 베어메탈 readiness 판정."""

    mode: ReadinessMode
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DoctorReport:
    generated_at: datetime
    checks: list[DoctorCheck]
    plan: DoctorPlan


def run_command(cmd: Sequence[str], timeout: float) -> CommandResult:
    """짧은 읽기 전용 진단 명령을 timeout 안에서 실행한다."""
    try:
        completed = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as e:
        return CommandResult(returncode=127, stderr=str(e))
    except subprocess.TimeoutExpired as e:
        return CommandResult(
            returncode=124,
            stdout=_timeout_text(e.stdout),
            stderr=_timeout_text(e.stderr) or f"timed out after {timeout:g}s",
            timed_out=True,
        )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def build_doctor_report(
    *,
    now: datetime | None = None,
    dev_paths: Sequence[str] | None = None,
    command_runner: CommandRunner = run_command,
    nvml_probe: NVMLProbe | None = None,
    which: Which = shutil.which,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> DoctorReport:
    """로컬 bare-metal readiness probe 를 실행하고 host/unsupported 판정을 만든다."""
    probe_nvml_func = nvml_probe if nvml_probe is not None else probe_nvml
    generated_at = now if now is not None else datetime.now(UTC)

    os_info, os_check = probe_os()
    device_info, device_check = probe_nvidia_devices(dev_paths)
    smi_info, smi_check = probe_nvidia_smi(which=which, command_runner=command_runner)
    nvml_info = probe_nvml_func()
    nvml_check = check_nvml(nvml_info)
    database_info, database_check = probe_default_db(db_path)
    facts = DetectionFacts(
        os=os_info,
        devices=device_info,
        nvidia_smi=smi_info,
        nvml=nvml_info,
        database=database_info,
    )
    plan = select_doctor_plan(facts)
    return DoctorReport(
        generated_at=generated_at,
        checks=[
            os_check,
            device_check,
            smi_check,
            nvml_check,
            database_check,
        ],
        plan=plan,
    )


def probe_os() -> tuple[OSInfo, DoctorCheck]:
    python_version = ".".join(str(part) for part in sys.version_info[:3])
    system = platform.system() or "unknown"
    release = platform.release() or "unknown"
    info = OSInfo(
        system=system,
        release=release,
        platform_string=platform.platform(),
        python_version=python_version,
        python_executable=sys.executable,
        is_linux=system.lower() == "linux",
    )
    status: CheckStatus = "ok" if info.is_linux else "error"
    summary = f"{info.system} {info.release}, Python {info.python_version}"
    return info, DoctorCheck(
        id="os",
        name="OS/kernel/Python",
        status=status,
        summary=summary,
        details={
            "system": info.system,
            "release": info.release,
            "platform": info.platform_string,
            "python_version": info.python_version,
            "python_executable": info.python_executable,
        },
    )


def probe_nvidia_devices(
    dev_paths: Sequence[str] | None = None,
) -> tuple[NvidiaDeviceInfo, DoctorCheck]:
    paths = sorted(dev_paths if dev_paths is not None else glob.glob("/dev/nvidia*"))
    gpu_paths = [path for path in paths if _is_nvidia_gpu_device(path)]
    control_paths = [path for path in paths if path not in gpu_paths]
    info = NvidiaDeviceInfo(
        paths=list(paths),
        gpu_device_paths=gpu_paths,
        control_device_paths=control_paths,
    )
    if gpu_paths:
        status: CheckStatus = "ok"
        summary = f"{len(gpu_paths)} GPU device files"
    elif paths:
        status = "warning"
        summary = "found /dev/nvidia* entries, but no GPU device files"
    else:
        status = "error"
        summary = "no /dev/nvidia* entries found"
    return info, DoctorCheck(
        id="nvidia_devices",
        name="/dev/nvidia*",
        status=status,
        summary=summary,
        details={
            "paths": info.paths,
            "gpu_device_paths": info.gpu_device_paths,
            "control_device_paths": info.control_device_paths,
        },
    )


def probe_nvidia_smi(
    *,
    which: Which,
    command_runner: CommandRunner,
) -> tuple[NvidiaSMIInfo, DoctorCheck]:
    path = which("nvidia-smi")
    if path is None:
        info = NvidiaSMIInfo(found=False)
        return info, DoctorCheck(
            id="nvidia_smi",
            name="nvidia-smi",
            status="error",
            summary="not found on PATH",
            details={"found": False},
        )

    result = command_runner(["nvidia-smi", "-L"], DEFAULT_COMMAND_TIMEOUT_SECONDS)
    gpu_lines, mig_lines = _nvidia_smi_list_lines(result.stdout)
    info = NvidiaSMIInfo(
        found=True,
        path=path,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        timed_out=result.timed_out,
        gpu_lines=gpu_lines,
        mig_lines=mig_lines,
    )
    if result.returncode == 0 and (gpu_lines or mig_lines):
        status: CheckStatus = "ok"
        summary = _nvidia_smi_summary(gpu_lines, mig_lines)
    elif result.timed_out:
        status = "error"
        summary = f"`nvidia-smi -L` timed out after {DEFAULT_COMMAND_TIMEOUT_SECONDS:g}s"
    elif result.returncode == 0:
        status = "error"
        summary = "`nvidia-smi -L` returned no GPUs"
    else:
        status = "error"
        summary = f"`nvidia-smi -L` failed: {_short_error(result)}"
    return info, DoctorCheck(
        id="nvidia_smi",
        name="nvidia-smi",
        status=status,
        summary=summary,
        details={
            "found": True,
            "path": path,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "timed_out": result.timed_out,
            "gpu_lines": gpu_lines,
            "mig_lines": mig_lines,
        },
    )


def probe_nvml() -> NVMLInfo:
    try:
        nvml = _load_pynvml()
    except NVMLNotAvailableError as e:
        return NVMLInfo(loadable=False, initialized=False, error=str(e))

    initialized = False
    try:
        nvml.nvmlInit()
        initialized = True
        return NVMLInfo(
            loadable=True,
            initialized=True,
            device_count=int(nvml.nvmlDeviceGetCount()),
            driver_version=_decode(nvml.nvmlSystemGetDriverVersion()),
        )
    except nvml.NVMLError as e:
        return NVMLInfo(loadable=True, initialized=False, error=nvml_init_error_message(e, nvml))
    except Exception as e:  # pragma: no cover - 플랫폼별 NVML 실패 방어.
        return NVMLInfo(loadable=True, initialized=False, error=str(e))
    finally:
        if initialized:
            with contextlib.suppress(nvml.NVMLError):
                nvml.nvmlShutdown()


def check_nvml(info: NVMLInfo) -> DoctorCheck:
    details: dict[str, object] = {
        "loadable": info.loadable,
        "initialized": info.initialized,
        "device_count": info.device_count,
        "driver_version": info.driver_version,
        "error": info.error,
    }
    if not info.loadable:
        error = _one_line(info.error)
        summary = (
            error
            if error.startswith("pynvml is not importable")
            else f"pynvml is not importable: {error}"
        )
        return DoctorCheck(
            id="nvml",
            name="NVML",
            status="error",
            summary=summary,
            details=details,
        )
    if not info.initialized:
        return DoctorCheck(
            id="nvml",
            name="NVML",
            status="error",
            summary=f"loadable but init failed: {_one_line(info.error)}",
            details=details,
        )
    if info.device_count and info.device_count > 0:
        driver = f", driver {info.driver_version}" if info.driver_version else ""
        return DoctorCheck(
            id="nvml",
            name="NVML",
            status="ok",
            summary=f"initialized, GPU count={info.device_count}{driver}",
            details=details,
        )
    return DoctorCheck(
        id="nvml",
        name="NVML",
        status="error",
        summary="initialized, GPU count=0",
        details=details,
    )


def probe_default_db(db_path: str | Path = DEFAULT_DB_PATH) -> tuple[DatabaseInfo, DoctorCheck]:
    path = expand_path(db_path)
    display_path = str(path)
    parent = path.parent
    is_default = is_default_db_path(path)
    try:
        exists = path.exists()
        is_file = path.is_file() if exists else False
        parent_exists = parent.exists()
        # ACL, root_squash, capability 기반 권한에서는 warning-only 근사값일 수 있다.
        parent_writable = parent_exists and os.access(parent, os.W_OK)
        size_bytes = path.stat().st_size if exists and is_file else None
        error = None
    except OSError as e:
        info = DatabaseInfo(
            path=display_path,
            is_default=is_default,
            exists=False,
            is_file=False,
            parent_exists=False,
            parent_writable=False,
            error=str(e),
        )
        return info, DoctorCheck(
            id="default_db",
            name="default DB path",
            status="error",
            summary=f"cannot inspect {display_path}: {e}",
            details=_database_details(info),
        )

    info = DatabaseInfo(
        path=display_path,
        is_default=is_default,
        exists=exists,
        is_file=is_file,
        parent_exists=parent_exists,
        parent_writable=parent_writable,
        size_bytes=size_bytes,
        error=error,
    )
    if exists and is_file and is_default:
        status: CheckStatus = "ok"
        summary = "present; daemon will append, report can read it"
    elif exists and is_file:
        status = "warning"
        summary = "present; daemon will refuse this path, report can read it"
    elif exists:
        status = "error"
        summary = "present but is not a regular file"
    elif not parent_exists and is_default:
        status = "ok"
        summary = f"absent, parent directory will be created: {parent}"
    elif not parent_exists:
        status = "error"
        summary = f"absent, parent directory does not exist: {parent}"
    elif not parent_writable:
        status = "warning"
        summary = f"absent, parent directory may not be writable: {parent}"
    else:
        status = "ok"
        summary = "absent, ready for a new daemon run"
    return info, DoctorCheck(
        id="default_db",
        name="default DB path",
        status=status,
        summary=summary,
        details=_database_details(info),
    )


def select_doctor_plan(facts: DetectionFacts) -> DoctorPlan:
    blockers = _unsupported_blockers(facts)
    warnings = _host_warnings(facts)
    if blockers:
        return DoctorPlan(
            mode="unsupported",
            reasons=[
                "This command only audits the local machine, and host readiness is incomplete."
            ],
            blockers=blockers,
            warnings=warnings,
        )

    return DoctorPlan(
        mode="host",
        reasons=[
            f"Local NVML initialized and sees {facts.nvml.device_count} GPU(s).",
            "`nvidia-smi -L` lists GPUs on this machine.",
            "The 1.0 workflow writes local NVML samples to a local SQLite database.",
        ],
        warnings=warnings,
    )


def render_doctor(report: DoctorReport) -> str:
    checks = {check.id: check for check in report.checks}
    lines = [
        "gua doctor",
        "",
        "Scope:",
        "  machine: local",
        "",
        "Host:",
    ]
    _append_check_line(lines, checks["os"])
    lines.extend(["", "Host GPU:"])
    _append_check_line(lines, checks["nvidia_devices"])
    _append_check_line(lines, checks["nvidia_smi"])
    _append_check_line(lines, checks["nvml"])

    db_check = checks["default_db"]
    db_path = str(db_check.details.get("path", DEFAULT_DB_PATH))
    path_label = "default" if db_check.details.get("is_default") is True else "target"
    if db_check.status == "ok":
        db_status = db_check.summary
    else:
        db_status = f"{db_check.status}, {db_check.summary}"
    lines.extend(
        [
            "",
            "Database:",
            f"  {path_label}: {db_path}",
            f"  status: {db_status}",
        ]
    )

    commands = _recommended_commands_for(report)
    if commands:
        lines.extend(["", "Recommended commands:"])
        collect = commands.get("collect")
        if collect:
            lines.append(f"  collect: {collect}")
            lines.append(f"  report after collecting: {commands['report']}")
        else:
            lines.append(f"  report existing data: {commands['report']}")

    _append_section(lines, "Fix", _fixes_for(report))
    _append_section(lines, "Notes", report.plan.warnings)
    return "\n".join(lines)


def doctor_report_to_dict(report: DoctorReport) -> dict[str, object]:
    data: dict[str, object] = {
        "schema_version": 1,
        "generated_at": report.generated_at.isoformat(),
        "scope": {"machine": "local"},
        "read_only": True,
        "no_system_changes": True,
        "checks": [doctor_check_to_dict(check) for check in report.checks],
        "plan": doctor_plan_to_dict(report.plan),
    }
    if report.plan.mode == "host":
        data["recommended_commands"] = _recommended_commands_for(report)
    return data


def doctor_check_to_dict(check: DoctorCheck) -> dict[str, object]:
    return {
        "id": check.id,
        "name": check.name,
        "status": check.status,
        "summary": check.summary,
        "details": check.details,
    }


def doctor_plan_to_dict(plan: DoctorPlan) -> dict[str, object]:
    return {
        "mode": plan.mode,
        "reasons": plan.reasons,
        "blockers": plan.blockers,
        "warnings": plan.warnings,
    }


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _is_nvidia_gpu_device(path: str) -> bool:
    name = Path(path).name
    return name.startswith("nvidia") and name.removeprefix("nvidia").isdigit()


def _nvidia_smi_list_lines(stdout: str) -> tuple[list[str], list[str]]:
    gpu_lines: list[str] = []
    mig_lines: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("GPU "):
            gpu_lines.append(stripped)
        elif stripped.startswith("MIG "):
            mig_lines.append(stripped)
    return gpu_lines, mig_lines


def _nvidia_smi_summary(gpu_lines: Sequence[str], mig_lines: Sequence[str]) -> str:
    parts: list[str] = []
    if gpu_lines:
        suffix = "" if len(gpu_lines) == 1 else "s"
        parts.append(f"{len(gpu_lines)} GPU{suffix}")
    if mig_lines:
        suffix = "" if len(mig_lines) == 1 else "s"
        parts.append(f"{len(mig_lines)} MIG instance{suffix}")
    return ", ".join(parts)


def _recommended_commands_for(report: DoctorReport) -> dict[str, str]:
    if report.plan.mode != "host":
        return {}

    checks = {check.id: check for check in report.checks}
    database = checks["default_db"].details
    db_path = str(database.get("path", DEFAULT_DB_PATH))
    report_command = _report_command(db_path)
    if database.get("exists") is True and database.get("is_default") is not True:
        return {"report": report_command}
    if database.get("parent_writable") is False and database.get("is_default") is not True:
        return {}
    return {
        "collect": _collect_command(db_path),
        "report": report_command,
    }


def _collect_command(db_path: str) -> str:
    if is_default_db_path(db_path):
        return COLLECT_COMMAND
    return f"gua daemon --db {shlex.quote(db_path)} --interval 30s"


def _report_command(db_path: str) -> str:
    if is_default_db_path(db_path):
        return REPORT_COMMAND
    return f"gua report --db {shlex.quote(db_path)} --since 1h"


def _short_error(result: CommandResult) -> str:
    text = result.stderr.strip() or result.stdout.strip()
    if not text:
        text = f"exit {result.returncode}"
    if len(text) > 160:
        return text[:157] + "..."
    return text


def _unsupported_blockers(facts: DetectionFacts) -> list[str]:
    blockers: list[str] = []
    if not facts.os.is_linux:
        blockers.append(f"{facts.os.system} is not a supported collector host OS.")
    if not facts.devices.paths:
        blockers.append("No /dev/nvidia* device files were found on this machine.")
    elif not facts.devices.gpu_device_paths:
        blockers.append("No /dev/nvidiaN GPU device files were found on this machine.")
    if not facts.nvidia_smi.found:
        blockers.append("nvidia-smi is not installed or not on PATH.")
    elif facts.nvidia_smi.timed_out:
        blockers.append("`nvidia-smi -L` timed out.")
    elif facts.nvidia_smi.returncode != 0:
        blockers.append(f"`nvidia-smi -L` failed: {_one_line(facts.nvidia_smi.stderr)}")
    elif not facts.nvidia_smi.gpu_lines and not facts.nvidia_smi.mig_lines:
        blockers.append("`nvidia-smi -L` returned no GPUs.")
    if not facts.nvml.loadable:
        blockers.append(f"NVML could not be loaded: {_one_line(facts.nvml.error)}")
    elif not facts.nvml.initialized:
        blockers.append(f"NVML could not be initialized: {_one_line(facts.nvml.error)}")
    elif facts.nvml.device_count == 0:
        blockers.append("NVML initialized but reported zero GPUs.")
    if facts.database.exists and not facts.database.is_file:
        blockers.append(f"{facts.database.path} exists but is not a regular file.")
    elif (
        not facts.database.exists
        and not facts.database.parent_exists
        and not facts.database.is_default
    ):
        blockers.append(f"The parent directory for {facts.database.path} does not exist.")
    return blockers


def _host_warnings(facts: DetectionFacts) -> list[str]:
    warnings: list[str] = []
    if facts.database.exists and facts.database.is_file and not facts.database.is_default:
        warnings.append(
            f"{facts.database.path} already exists; `gua daemon --db PATH` will refuse "
            "this path until it is removed or another --db path is provided."
        )
    elif (
        not facts.database.exists
        and facts.database.parent_exists
        and not facts.database.parent_writable
    ):
        warnings.append(
            f"The parent directory for {facts.database.path} may not be writable by this user."
        )
    return warnings


def _fixes_for(report: DoctorReport) -> list[str]:
    checks = {check.id: check for check in report.checks}
    fixes: list[str] = []
    if checks["nvidia_devices"].status == "error":
        fixes.append("Run on an NVIDIA host where /dev/nvidia* device files are visible.")
    smi = checks["nvidia_smi"]
    if smi.status == "error":
        if smi.details.get("timed_out") is True:
            fixes.append(
                "Investigate the `nvidia-smi -L` timeout; repair the hung driver/kernel "
                "state and rerun doctor."
            )
        elif smi.details.get("found") is False:
            fixes.append(
                "Install NVIDIA driver utilities so `nvidia-smi -L` is on PATH and lists "
                "this host's GPUs."
            )
        else:
            fixes.append(
                "Repair NVIDIA driver utilities so `nvidia-smi -L` lists this host's GPUs."
            )
    nvml = checks["nvml"].details
    if nvml.get("loadable") is False:
        fixes.append("Reinstall the tool environment: uv tool install --force gpu-usage-audit")
    elif nvml.get("initialized") is False:
        fixes.append(
            "Install or repair the NVIDIA driver so libnvidia-ml.so.1 is available "
            "and matches the loaded kernel driver; verify with `nvidia-smi -L`."
        )
    elif nvml.get("device_count") == 0:
        fixes.append(
            "NVML reports zero GPUs; verify that this user can see GPU devices and that "
            "`nvidia-smi -L` lists the same host GPUs."
        )
    database = checks["default_db"]
    if database.status == "error":
        fixes.append("Choose a regular writable --db PATH whose parent directory exists.")
    return fixes


def _append_check_line(lines: list[str], check: DoctorCheck) -> None:
    lines.append(f"  {check.name}: {check.status}, {check.summary}")


def _append_section(lines: list[str], title: str, values: Sequence[str]) -> None:
    if not values:
        return
    lines.append("")
    lines.append(f"{title}:")
    for value in values:
        lines.append(f"  - {value}")


def _database_details(info: DatabaseInfo) -> dict[str, object]:
    return {
        "path": info.path,
        "is_default": info.is_default,
        "exists": info.exists,
        "is_file": info.is_file,
        "parent_exists": info.parent_exists,
        "parent_writable": info.parent_writable,
        "size_bytes": info.size_bytes,
        "error": info.error,
    }


def _one_line(value: str | None) -> str:
    if not value:
        return "unknown"
    return value.strip().splitlines()[0]
