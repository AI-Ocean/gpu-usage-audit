"""`gua` command surface 를 위한 읽기 전용 환경 진단."""

from __future__ import annotations

import contextlib
import glob
import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .model import PlanConfidence, PlannedAction, RuntimePlan, SchedulerSource
from .nvml import NVMLNotAvailableError, _decode, _load_pynvml

type CheckStatus = Literal["ok", "warning", "error", "skipped"]
type PathExists = Callable[[str], bool]
type Which = Callable[[str], str | None]

DEFAULT_COMMAND_TIMEOUT_SECONDS = 3.0
DEFAULT_SLURM_CONFIG_PATHS = (
    "/etc/slurm/slurm.conf",
    "/etc/slurm/gres.conf",
    "/etc/slurm-llnl/slurm.conf",
    "/etc/slurm-llnl/gres.conf",
)
DEFAULT_CONTAINER_HOOK_PATHS = (
    "/usr/share/containers/oci/hooks.d/oci-nvidia-hook.json",
    "/etc/containers/oci/hooks.d/oci-nvidia-hook.json",
)


@dataclass(slots=True)
class CommandResult:
    """probe 테스트를 단순하게 만드는 작은 subprocess 결과 래퍼."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


type CommandRunner = Callable[[Sequence[str], float], CommandResult]


@dataclass(slots=True)
class DoctorCheck:
    """읽기 전용 진단 항목 하나."""

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
class NVMLInfo:
    loadable: bool
    initialized: bool
    device_count: int | None = None
    driver_version: str | None = None
    error: str | None = None


type NVMLProbe = Callable[[], NVMLInfo]


@dataclass(slots=True)
class KubectlInfo:
    found: bool
    path: str | None = None
    auth_ok: bool = False
    auth_summary: str = "kubectl not found"
    auth_returncode: int | None = None


@dataclass(slots=True)
class KubernetesRuntimeInfo:
    inside_cluster: bool
    nvidia_runtime_classes: list[str] = field(default_factory=list)
    gpu_capacity_nodes: list[str] = field(default_factory=list)
    gpu_label_nodes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def has_signal(self) -> bool:
        return bool(
            self.inside_cluster
            or self.nvidia_runtime_classes
            or self.gpu_capacity_nodes
            or self.gpu_label_nodes
        )


@dataclass(slots=True)
class SlurmInfo:
    command_paths: dict[str, str]
    config_paths: list[str]
    scontrol_config_ok: bool = False
    scontrol_config_mentions_gres: bool = False
    scontrol_error: str | None = None

    @property
    def has_signal(self) -> bool:
        return bool(self.command_paths or self.config_paths or self.scontrol_config_ok)


@dataclass(slots=True)
class ContainerFallbackInfo:
    engine_paths: dict[str, str]
    nvidia_command_paths: dict[str, str]
    hook_paths: list[str]
    docker_runtime_signal: bool = False
    podman_runtime_signal: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def has_access_blocker(self) -> bool:
        return any("access required:" in error for error in self.errors)

    @property
    def has_signal(self) -> bool:
        return bool(
            not self.has_access_blocker
            and self.engine_paths
            and (
                self.nvidia_command_paths
                or self.hook_paths
                or self.docker_runtime_signal
                or self.podman_runtime_signal
            )
        )


@dataclass(slots=True)
class DetectionFacts:
    os: OSInfo
    devices: NvidiaDeviceInfo
    nvml: NVMLInfo
    kubectl: KubectlInfo
    kubernetes: KubernetesRuntimeInfo
    slurm: SlurmInfo
    container: ContainerFallbackInfo


@dataclass(slots=True)
class DoctorReport:
    generated_at: datetime
    checks: list[DoctorCheck]
    plan: RuntimePlan


def run_command(cmd: Sequence[str], timeout: float) -> CommandResult:
    """읽기 전용 명령을 짧은 timeout 안에서 실행한다."""
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
    env: Mapping[str, str] | None = None,
    path_exists: PathExists | None = None,
    slurm_config_paths: Sequence[str] = DEFAULT_SLURM_CONFIG_PATHS,
    container_hook_paths: Sequence[str] = DEFAULT_CONTAINER_HOOK_PATHS,
) -> DoctorReport:
    """모든 doctor probe 를 실행하고 추천 RuntimePlan 을 만든다.

    모든 probe 는 읽기 전용이다. 외부 명령도 `kubectl auth can-i`,
    `kubectl get ...`, `scontrol show config`, container-runtime info 같은
    짧은 진단 호출로 제한한다.
    """
    env_map = env if env is not None else dict(os.environ)
    exists = path_exists if path_exists is not None else _path_exists
    probe_nvml_func = nvml_probe if nvml_probe is not None else probe_nvml
    generated_at = now if now is not None else datetime.now(UTC)

    os_info, os_check = probe_os()
    device_info, device_check = probe_nvidia_devices(dev_paths)
    nvml_info = probe_nvml_func()
    nvml_check = check_nvml(nvml_info)
    kubectl_info, kubectl_check = probe_kubectl(which=which, command_runner=command_runner)
    kubernetes_info, kubernetes_check = probe_kubernetes_runtime(
        kubectl=kubectl_info,
        command_runner=command_runner,
        env=env_map,
    )
    slurm_info, slurm_check = probe_slurm(
        which=which,
        command_runner=command_runner,
        env=env_map,
        path_exists=exists,
        config_paths=slurm_config_paths,
    )
    container_info, container_check = probe_container_fallback(
        which=which,
        command_runner=command_runner,
        path_exists=exists,
        hook_paths=container_hook_paths,
    )
    facts = DetectionFacts(
        os=os_info,
        devices=device_info,
        nvml=nvml_info,
        kubectl=kubectl_info,
        kubernetes=kubernetes_info,
        slurm=slurm_info,
        container=container_info,
    )
    plan = select_runtime_plan(facts)
    return DoctorReport(
        generated_at=generated_at,
        checks=[
            os_check,
            device_check,
            nvml_check,
            kubectl_check,
            kubernetes_check,
            slurm_check,
            container_check,
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
    if paths:
        status: CheckStatus = "ok"
        summary = f"{len(paths)} entries ({len(gpu_paths)} GPU device files)"
    else:
        status = "warning"
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
        return NVMLInfo(loadable=True, initialized=False, error=str(e))
    except Exception as e:  # pragma: no cover - defensive for platform-specific NVML failures.
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
        return DoctorCheck(
            id="nvml",
            name="host NVML",
            status="warning",
            summary=f"unavailable: {_one_line(info.error)}",
            details=details,
        )
    if not info.initialized:
        return DoctorCheck(
            id="nvml",
            name="host NVML",
            status="warning",
            summary=f"loadable but init failed: {_one_line(info.error)}",
            details=details,
        )
    if info.device_count and info.device_count > 0:
        driver = f", driver {info.driver_version}" if info.driver_version else ""
        return DoctorCheck(
            id="nvml",
            name="host NVML",
            status="ok",
            summary=f"initialized, GPU count={info.device_count}{driver}",
            details=details,
        )
    return DoctorCheck(
        id="nvml",
        name="host NVML",
        status="warning",
        summary="initialized, GPU count=0",
        details=details,
    )


def probe_kubectl(
    *,
    which: Which,
    command_runner: CommandRunner,
) -> tuple[KubectlInfo, DoctorCheck]:
    path = which("kubectl")
    if path is None:
        info = KubectlInfo(found=False)
        return info, DoctorCheck(
            id="kubectl",
            name="kubectl",
            status="skipped",
            summary="not found",
            details={"found": False},
        )

    result = command_runner(
        ["kubectl", "auth", "can-i", "get", "pods", "--all-namespaces"],
        DEFAULT_COMMAND_TIMEOUT_SECONDS,
    )
    stdout = result.stdout.strip().lower()
    auth_ok = result.returncode == 0 and stdout.startswith("yes")
    if auth_ok:
        status: CheckStatus = "ok"
        summary = "found and authenticated for pod reads"
    elif result.timed_out:
        status = "warning"
        summary = "found, auth check timed out"
    elif result.returncode == 0:
        status = "warning"
        summary = f"found, auth check returned {result.stdout.strip() or 'unknown'}"
    else:
        status = "warning"
        summary = f"found, auth check failed: {_short_error(result)}"
    info = KubectlInfo(
        found=True,
        path=path,
        auth_ok=auth_ok,
        auth_summary=summary,
        auth_returncode=result.returncode,
    )
    return info, DoctorCheck(
        id="kubectl",
        name="kubectl",
        status=status,
        summary=summary,
        details={
            "found": True,
            "path": path,
            "auth_ok": auth_ok,
            "auth_returncode": result.returncode,
            "auth_stdout": result.stdout.strip(),
            "auth_stderr": result.stderr.strip(),
            "timed_out": result.timed_out,
        },
    )


def probe_kubernetes_runtime(
    *,
    kubectl: KubectlInfo,
    command_runner: CommandRunner,
    env: Mapping[str, str],
) -> tuple[KubernetesRuntimeInfo, DoctorCheck]:
    inside_cluster = "KUBERNETES_SERVICE_HOST" in env
    status: CheckStatus
    if not kubectl.found:
        info = KubernetesRuntimeInfo(inside_cluster=inside_cluster)
        status = "warning" if inside_cluster else "skipped"
        summary = (
            "inside-cluster environment is present, but kubectl is not found"
            if inside_cluster
            else "skipped because kubectl is not found"
        )
        return info, DoctorCheck(
            id="kubernetes_runtime",
            name="Kubernetes runtime signal",
            status=status,
            summary=summary,
            details={"inside_cluster": inside_cluster},
        )

    errors: list[str] = []
    runtime_classes: list[str] = []
    gpu_capacity_nodes: list[str] = []
    gpu_label_nodes: list[str] = []

    if kubectl.auth_ok:
        runtime_result = command_runner(
            ["kubectl", "get", "runtimeclass", "-o", "json"],
            DEFAULT_COMMAND_TIMEOUT_SECONDS,
        )
        if runtime_result.returncode == 0:
            runtime_classes = _parse_nvidia_runtime_classes(runtime_result.stdout)
        else:
            errors.append(f"runtimeclass query failed: {_short_error(runtime_result)}")

        node_result = command_runner(
            ["kubectl", "get", "nodes", "-o", "json"],
            DEFAULT_COMMAND_TIMEOUT_SECONDS,
        )
        if node_result.returncode == 0:
            gpu_capacity_nodes, gpu_label_nodes = _parse_gpu_node_signals(node_result.stdout)
        else:
            errors.append(f"node query failed: {_short_error(node_result)}")
    else:
        errors.append("kubectl auth is not available")

    info = KubernetesRuntimeInfo(
        inside_cluster=inside_cluster,
        nvidia_runtime_classes=runtime_classes,
        gpu_capacity_nodes=gpu_capacity_nodes,
        gpu_label_nodes=gpu_label_nodes,
        errors=errors,
    )

    if info.has_signal:
        status = "ok"
        summary_parts: list[str] = []
        if inside_cluster:
            summary_parts.append("inside-cluster env")
        if runtime_classes:
            summary_parts.append(f"runtimeClass={','.join(runtime_classes)}")
        if gpu_capacity_nodes:
            summary_parts.append(f"GPU capacity nodes={len(gpu_capacity_nodes)}")
        if gpu_label_nodes:
            summary_parts.append(f"GPU label nodes={len(gpu_label_nodes)}")
        summary = "; ".join(summary_parts)
    elif errors:
        status = "warning"
        summary = "; ".join(errors)
    else:
        status = "skipped"
        summary = "no NVIDIA Kubernetes runtime or GPU node signal found"

    return info, DoctorCheck(
        id="kubernetes_runtime",
        name="Kubernetes runtime signal",
        status=status,
        summary=summary,
        details={
            "inside_cluster": inside_cluster,
            "nvidia_runtime_classes": runtime_classes,
            "gpu_capacity_nodes": gpu_capacity_nodes,
            "gpu_label_nodes": gpu_label_nodes,
            "errors": errors,
        },
    )


def probe_slurm(
    *,
    which: Which,
    command_runner: CommandRunner,
    env: Mapping[str, str],
    path_exists: PathExists,
    config_paths: Sequence[str],
) -> tuple[SlurmInfo, DoctorCheck]:
    command_paths = {
        name: path for name in ("sinfo", "scontrol", "squeue") if (path := which(name)) is not None
    }
    candidate_configs = list(config_paths)
    slurm_conf = env.get("SLURM_CONF")
    if slurm_conf:
        candidate_configs.insert(0, slurm_conf)
    existing_configs = [path for path in candidate_configs if path_exists(path)]

    scontrol_config_ok = False
    scontrol_mentions_gres = False
    scontrol_error: str | None = None
    if "scontrol" in command_paths:
        result = command_runner(
            ["scontrol", "show", "config"],
            DEFAULT_COMMAND_TIMEOUT_SECONDS,
        )
        if result.returncode == 0:
            scontrol_config_ok = True
            lowered = result.stdout.lower()
            scontrol_mentions_gres = "grestypes" in lowered or "gres.conf" in lowered
        else:
            scontrol_error = _short_error(result)

    info = SlurmInfo(
        command_paths=command_paths,
        config_paths=existing_configs,
        scontrol_config_ok=scontrol_config_ok,
        scontrol_config_mentions_gres=scontrol_mentions_gres,
        scontrol_error=scontrol_error,
    )
    if info.has_signal:
        status: CheckStatus = "ok"
        pieces: list[str] = []
        if command_paths:
            pieces.append("commands=" + ",".join(sorted(command_paths)))
        if existing_configs:
            pieces.append(f"config files={len(existing_configs)}")
        if scontrol_config_ok:
            pieces.append("scontrol config readable")
        summary = "; ".join(pieces)
    else:
        status = "skipped"
        summary = "no Slurm commands or config files detected"

    return info, DoctorCheck(
        id="slurm",
        name="Slurm command/config signal",
        status=status,
        summary=summary,
        details={
            "command_paths": command_paths,
            "config_paths": existing_configs,
            "scontrol_config_ok": scontrol_config_ok,
            "scontrol_config_mentions_gres": scontrol_mentions_gres,
            "scontrol_error": scontrol_error,
        },
    )


def probe_container_fallback(
    *,
    which: Which,
    command_runner: CommandRunner,
    path_exists: PathExists,
    hook_paths: Sequence[str],
) -> tuple[ContainerFallbackInfo, DoctorCheck]:
    engine_paths = {
        name: path for name in ("docker", "podman") if (path := which(name)) is not None
    }
    nvidia_command_paths = {
        name: path
        for name in ("nvidia-container-runtime", "nvidia-ctk")
        if (path := which(name)) is not None
    }
    existing_hooks = [path for path in hook_paths if path_exists(path)]
    errors: list[str] = []
    docker_runtime_signal = False
    podman_runtime_signal = False

    if "docker" in engine_paths:
        result = command_runner(
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            DEFAULT_COMMAND_TIMEOUT_SECONDS,
        )
        if result.returncode == 0:
            docker_runtime_signal = "nvidia" in result.stdout.lower()
        elif _looks_like_permission_denied(result):
            errors.append(f"docker daemon access required: {_short_error(result)}")
        else:
            errors.append(f"docker info failed: {_short_error(result)}")

    if "podman" in engine_paths:
        result = command_runner(
            ["podman", "info", "--format", "json"],
            DEFAULT_COMMAND_TIMEOUT_SECONDS,
        )
        if result.returncode == 0:
            lowered = result.stdout.lower()
            podman_runtime_signal = "nvidia" in lowered or "oci-nvidia-hook" in lowered
        elif _looks_like_permission_denied(result):
            errors.append(f"podman access required: {_short_error(result)}")
        else:
            errors.append(f"podman info failed: {_short_error(result)}")

    info = ContainerFallbackInfo(
        engine_paths=engine_paths,
        nvidia_command_paths=nvidia_command_paths,
        hook_paths=existing_hooks,
        docker_runtime_signal=docker_runtime_signal,
        podman_runtime_signal=podman_runtime_signal,
        errors=errors,
    )
    if info.has_signal:
        status: CheckStatus = "ok"
        summary = "NVIDIA container fallback signal detected"
    elif info.has_access_blocker:
        status = "warning"
        summary = "; ".join(error for error in errors if "access required:" in error)
    elif engine_paths or nvidia_command_paths or existing_hooks:
        status = "warning"
        summary = "container tooling detected, but NVIDIA fallback is incomplete"
    else:
        status = "skipped"
        summary = "no Docker/Podman NVIDIA fallback signal detected"

    return info, DoctorCheck(
        id="container_fallback",
        name="Docker/Podman NVIDIA fallback",
        status=status,
        summary=summary,
        details={
            "engine_paths": engine_paths,
            "nvidia_command_paths": nvidia_command_paths,
            "hook_paths": existing_hooks,
            "docker_runtime_signal": docker_runtime_signal,
            "podman_runtime_signal": podman_runtime_signal,
            "errors": errors,
        },
    )


def select_runtime_plan(facts: DetectionFacts) -> RuntimePlan:
    if not facts.os.is_linux:
        return RuntimePlan(
            mode="unsupported",
            telemetry="nvml",
            scheduler="none",
            confidence="high",
            reasons=[f"{facts.os.system} is not a supported collector host OS."],
            blockers=["Run gpu-usage-audit on a Linux host or Linux container."],
        )

    if (
        facts.nvml.initialized
        and facts.nvml.device_count is not None
        and facts.nvml.device_count > 0
    ):
        scheduler: SchedulerSource = "slurm" if facts.slurm.has_signal else "none"
        reasons = [f"Host NVML initialized and sees {facts.nvml.device_count} GPU(s)."]
        if scheduler == "slurm":
            reasons.append(
                "Slurm command/config signal was detected, so scheduler context is Slurm."
            )
        else:
            reasons.append("No Kubernetes or Slurm scheduler signal needs a different runtime.")
        host_warnings: list[str] = []
        if facts.kubernetes.inside_cluster:
            host_warnings.append(
                "Kubernetes in-cluster environment is present; host runtime means the collector sees the current namespace, not a managed DaemonSet yet."
            )
        if not facts.devices.gpu_device_paths:
            host_warnings.append(
                "NVML sees GPUs, but /dev/nvidia GPU device files were not listed in this namespace."
            )
        return RuntimePlan(
            mode="host-systemd",
            telemetry="nvml",
            scheduler=scheduler,
            confidence="high",
            reasons=reasons,
            warnings=host_warnings,
            required_privileges=[
                "permission to read NVML GPU and process state",
                "permission to run a long-lived host collector",
                "write access to the collector database path",
            ],
            actions=[
                PlannedAction(
                    name="host-collector",
                    summary="Would configure or run the host collector runtime.",
                    changes_system=True,
                )
            ],
        )

    if facts.kubernetes.has_signal and facts.kubectl.found and facts.kubectl.auth_ok:
        confidence: PlanConfidence = (
            "high"
            if (facts.kubernetes.nvidia_runtime_classes or facts.kubernetes.gpu_capacity_nodes)
            else "medium"
        )
        k8s_warnings: list[str] = []
        if not facts.kubernetes.nvidia_runtime_classes:
            k8s_warnings.append(
                "No NVIDIA RuntimeClass was detected; a DaemonSet may need cluster-specific runtime settings."
            )
        return RuntimePlan(
            mode="k8s-daemonset",
            telemetry="nvml",
            scheduler="k8s",
            confidence=confidence,
            reasons=[
                "Host NVML did not expose GPUs from this namespace.",
                "kubectl is available and Kubernetes NVIDIA runtime/GPU node signals were detected.",
            ],
            warnings=k8s_warnings,
            required_privileges=[
                "kubectl permission to inspect pods, nodes, and runtime classes",
                "future start permission to create Namespace, ServiceAccount, RBAC, ConfigMap, and DaemonSet resources",
                "permission to run a node-wide DaemonSet with GPU device access",
            ],
            actions=[
                PlannedAction(
                    name="k8s-daemonset",
                    summary="Would render and apply the Kubernetes collector DaemonSet.",
                    changes_system=True,
                )
            ],
        )

    if facts.container.has_signal:
        return RuntimePlan(
            mode="local-container",
            telemetry="nvml",
            scheduler="none",
            confidence="medium",
            reasons=[
                "Host NVML did not expose GPUs directly.",
                "Docker/Podman and NVIDIA container runtime signals were detected.",
            ],
            warnings=[
                "Local container mode is a fallback path and may provide weaker host process attribution."
            ],
            required_privileges=[
                "permission to run Docker or Podman containers with NVIDIA GPU device access",
                "write access to a host-mounted collector database path",
            ],
            actions=[
                PlannedAction(
                    name="local-container",
                    summary="Would run the collector in a local NVIDIA-enabled container.",
                    changes_system=True,
                )
            ],
        )

    blockers = _unsupported_blockers(facts)
    reasons = [
        "No immediately usable host, Kubernetes, or local container runtime path was detected."
    ]
    warnings = []
    if facts.slurm.has_signal:
        warnings.append(
            "Slurm was detected, but host NVML must initialize and see GPUs before Slurm audit can run."
        )
    if facts.kubernetes.has_signal and (not facts.kubectl.found or not facts.kubectl.auth_ok):
        warnings.append("Kubernetes signals were detected, but kubectl auth is not ready.")
    return RuntimePlan(
        mode="unsupported",
        telemetry="nvml",
        scheduler="none",
        confidence="low",
        reasons=reasons,
        blockers=blockers,
        warnings=warnings,
    )


def render_doctor(report: DoctorReport) -> str:
    lines = ["gua doctor", "", "Detected environment:"]
    for check in report.checks:
        lines.append(f"  {check.name}: [{check.status}] {check.summary}")
    lines.append("")
    lines.extend(render_runtime_plan(report.plan).splitlines())
    return "\n".join(lines)


def render_runtime_plan(plan: RuntimePlan) -> str:
    lines = [
        "Recommended plan:",
        f"  runtime: {plan.mode}",
        f"  telemetry: {plan.telemetry}",
        f"  scheduler: {plan.scheduler}",
        f"  confidence: {plan.confidence}",
    ]
    _append_section(lines, "Reasons", plan.reasons)
    _append_section(lines, "Blockers", plan.blockers)
    _append_section(lines, "Warnings", plan.warnings)
    _append_section(lines, "Required privileges", plan.required_privileges)
    if plan.actions:
        lines.append("")
        lines.append("Planned actions:")
        for action in plan.actions:
            suffix = " (would change system state)" if action.changes_system else ""
            lines.append(f"  - {action.name}: {action.summary}{suffix}")
    return "\n".join(lines)


def doctor_report_to_dict(report: DoctorReport) -> dict[str, object]:
    return {
        "schema_version": 1,
        "generated_at": report.generated_at.isoformat(),
        "read_only": True,
        "no_system_changes": True,
        "checks": [doctor_check_to_dict(check) for check in report.checks],
        "plan": runtime_plan_to_dict(report.plan),
    }


def doctor_check_to_dict(check: DoctorCheck) -> dict[str, object]:
    return {
        "id": check.id,
        "name": check.name,
        "status": check.status,
        "summary": check.summary,
        "details": check.details,
    }


def runtime_plan_to_dict(plan: RuntimePlan) -> dict[str, object]:
    return {
        "mode": plan.mode,
        "telemetry": plan.telemetry,
        "scheduler": plan.scheduler,
        "confidence": plan.confidence,
        "reasons": plan.reasons,
        "blockers": plan.blockers,
        "warnings": plan.warnings,
        "required_privileges": plan.required_privileges,
        "actions": [
            {
                "name": action.name,
                "summary": action.summary,
                "changes_system": action.changes_system,
            }
            for action in plan.actions
        ],
    }


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _path_exists(path: str) -> bool:
    return Path(path).exists()


def _is_nvidia_gpu_device(path: str) -> bool:
    name = Path(path).name
    return name.startswith("nvidia") and name.removeprefix("nvidia").isdigit()


def _short_error(result: CommandResult) -> str:
    text = result.stderr.strip() or result.stdout.strip()
    if not text:
        text = f"exit {result.returncode}"
    if len(text) > 160:
        return text[:157] + "..."
    return text


def _looks_like_permission_denied(result: CommandResult) -> bool:
    text = f"{result.stderr}\n{result.stdout}".lower()
    permission_markers = (
        "permission denied",
        "access denied",
        "got permission denied",
        "cannot connect to the docker daemon",
        "is the docker daemon running",
        "connect: permission denied",
    )
    return any(marker in text for marker in permission_markers)


def _json_items(stdout: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    items = data.get("items")
    if not isinstance(items, list):
        return []
    dict_items: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            dict_items.append(item)
    return dict_items


def _parse_nvidia_runtime_classes(stdout: str) -> list[str]:
    names: list[str] = []
    for item in _json_items(stdout):
        name = _metadata_name(item)
        if name and "nvidia" in name.lower():
            names.append(name)
    return sorted(names)


def _parse_gpu_node_signals(stdout: str) -> tuple[list[str], list[str]]:
    capacity_nodes: list[str] = []
    label_nodes: list[str] = []
    for item in _json_items(stdout):
        name = _metadata_name(item) or "unknown"
        status = item.get("status")
        if isinstance(status, dict):
            capacity = status.get("capacity")
            if isinstance(capacity, dict) and _positive_quantity(capacity.get("nvidia.com/gpu")):
                capacity_nodes.append(name)
        metadata = item.get("metadata")
        if isinstance(metadata, dict):
            labels = metadata.get("labels")
            if isinstance(labels, dict) and _has_gpu_node_label(labels):
                label_nodes.append(name)
    return sorted(capacity_nodes), sorted(label_nodes)


def _metadata_name(item: Mapping[str, Any]) -> str | None:
    metadata = item.get("metadata")
    if not isinstance(metadata, dict):
        return None
    name = metadata.get("name")
    return name if isinstance(name, str) else None


def _positive_quantity(value: object) -> bool:
    if isinstance(value, int):
        return value > 0
    if not isinstance(value, str):
        return False
    try:
        return int(value) > 0
    except ValueError:
        return False


def _has_gpu_node_label(labels: Mapping[object, object]) -> bool:
    gpu_labels = {
        "nvidia.com/gpu.present",
        "feature.node.kubernetes.io/pci-10de.present",
    }
    for key, value in labels.items():
        if not isinstance(key, str) or key not in gpu_labels:
            continue
        if isinstance(value, str):
            if value.lower() in {"true", "1", "yes"}:
                return True
        elif value is True:
            return True
    return False


def _unsupported_blockers(facts: DetectionFacts) -> list[str]:
    blockers: list[str] = []
    if not facts.devices.paths:
        blockers.append("No /dev/nvidia* device files were found.")
    if not facts.nvml.loadable:
        blockers.append(f"NVML could not be loaded: {_one_line(facts.nvml.error)}")
    elif not facts.nvml.initialized:
        blockers.append(f"NVML could not be initialized: {_one_line(facts.nvml.error)}")
    elif facts.nvml.device_count == 0:
        blockers.append("NVML initialized but reported zero GPUs.")
    if not facts.kubectl.found:
        blockers.append("kubectl is not installed or not on PATH.")
    elif facts.kubernetes.has_signal and not facts.kubectl.auth_ok:
        blockers.append("kubectl auth is not ready for Kubernetes inspection.")
    if not facts.container.has_signal:
        blockers.append("No Docker/Podman NVIDIA fallback was detected.")
    return blockers


def _append_section(lines: list[str], title: str, values: Sequence[str]) -> None:
    if not values:
        return
    lines.append("")
    lines.append(f"{title}:")
    for value in values:
        lines.append(f"  - {value}")


def _one_line(value: str | None) -> str:
    if not value:
        return "unknown"
    return value.strip().splitlines()[0]
