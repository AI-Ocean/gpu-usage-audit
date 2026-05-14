"""런타임 doctor 감지와 plan 선택 테스트."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence

from gpu_usage_audit.doctor import (
    CommandResult,
    NVMLInfo,
    build_doctor_report,
    doctor_report_to_dict,
    render_doctor,
)


class FakeRunner:
    def __init__(self, responses: Mapping[tuple[str, ...], CommandResult]) -> None:
        self._responses = dict(responses)
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, cmd: Sequence[str], timeout: float) -> CommandResult:
        del timeout
        key = tuple(cmd)
        self.calls.append(key)
        return self._responses.get(key, CommandResult(returncode=1, stderr="unexpected command"))


def test_build_doctor_report_recommends_host_slurm_plan() -> None:
    runner = FakeRunner(
        {
            ("scontrol", "show", "config"): CommandResult(
                returncode=0,
                stdout="GresTypes=gpu\nGresPluginDir=/usr/lib/slurm\n",
            )
        }
    )

    report = build_doctor_report(
        dev_paths=["/dev/nvidia0", "/dev/nvidia1", "/dev/nvidiactl"],
        nvml_probe=lambda: NVMLInfo(
            loadable=True,
            initialized=True,
            device_count=2,
            driver_version="560.35.05",
        ),
        which=_which({"scontrol": "/usr/bin/scontrol"}),
        command_runner=runner,
        env={},
        path_exists=_exists(set()),
        slurm_config_paths=(),
        container_hook_paths=(),
    )

    assert report.plan.mode == "host-systemd"
    assert report.plan.telemetry == "nvml"
    assert report.plan.scheduler == "slurm"
    assert report.plan.confidence == "high"
    assert ("scontrol", "show", "config") in runner.calls
    assert "Slurm command/config signal" in render_doctor(report)


def test_build_doctor_report_recommends_k8s_daemonset_plan() -> None:
    runner = FakeRunner(
        {
            (
                "kubectl",
                "auth",
                "can-i",
                "get",
                "pods",
                "--all-namespaces",
            ): CommandResult(returncode=0, stdout="yes\n"),
            ("kubectl", "get", "runtimeclass", "-o", "json"): CommandResult(
                returncode=0,
                stdout=json.dumps({"items": [{"metadata": {"name": "nvidia"}}]}),
            ),
            ("kubectl", "get", "nodes", "-o", "json"): CommandResult(
                returncode=0,
                stdout=json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {
                                    "name": "gpu-node-a",
                                    "labels": {"nvidia.com/gpu.present": "true"},
                                },
                                "status": {"capacity": {"nvidia.com/gpu": "4"}},
                            }
                        ]
                    }
                ),
            ),
        }
    )

    report = build_doctor_report(
        dev_paths=[],
        nvml_probe=lambda: NVMLInfo(
            loadable=True,
            initialized=True,
            device_count=0,
            driver_version="560.35.05",
        ),
        which=_which({"kubectl": "/usr/bin/kubectl"}),
        command_runner=runner,
        env={},
        path_exists=_exists(set()),
        slurm_config_paths=(),
        container_hook_paths=(),
    )

    assert [check.id for check in report.checks] == [
        "os",
        "nvidia_devices",
        "nvml",
        "kubectl",
        "kubernetes_runtime",
        "slurm",
        "container_fallback",
    ]
    assert report.plan.mode == "k8s-daemonset"
    assert report.plan.scheduler == "k8s"
    assert report.plan.confidence == "high"
    data = doctor_report_to_dict(report)
    assert data["read_only"] is True
    assert data["no_system_changes"] is True
    json.dumps(data)


def test_build_doctor_report_recommends_local_container_fallback() -> None:
    runner = FakeRunner(
        {
            ("docker", "info", "--format", "{{json .Runtimes}}"): CommandResult(
                returncode=0,
                stdout='{"runc":{},"nvidia":{}}',
            )
        }
    )

    report = build_doctor_report(
        dev_paths=[],
        nvml_probe=lambda: NVMLInfo(
            loadable=True,
            initialized=False,
            error="driver not visible",
        ),
        which=_which(
            {
                "docker": "/usr/bin/docker",
                "nvidia-container-runtime": "/usr/bin/nvidia-container-runtime",
            }
        ),
        command_runner=runner,
        env={},
        path_exists=_exists(set()),
        slurm_config_paths=(),
        container_hook_paths=(),
    )

    assert report.plan.mode == "local-container"
    assert report.plan.scheduler == "none"
    assert report.plan.confidence == "medium"


def test_build_doctor_report_marks_unsupported_when_no_runtime_signal() -> None:
    report = build_doctor_report(
        dev_paths=[],
        nvml_probe=lambda: NVMLInfo(
            loadable=False,
            initialized=False,
            error="pynvml not installed\ninstall extra",
        ),
        which=_which({}),
        command_runner=FakeRunner({}),
        env={},
        path_exists=_exists(set()),
        slurm_config_paths=(),
        container_hook_paths=(),
    )

    assert report.plan.mode == "unsupported"
    assert any("No /dev/nvidia" in blocker for blocker in report.plan.blockers)
    assert any("pynvml not installed" in blocker for blocker in report.plan.blockers)
    rendered = render_doctor(report)
    assert "Recommended plan:" in rendered
    assert "runtime: unsupported" in rendered


def test_build_doctor_report_warns_when_host_plan_is_inside_kubernetes() -> None:
    report = build_doctor_report(
        dev_paths=["/dev/nvidia0", "/dev/nvidiactl"],
        nvml_probe=lambda: NVMLInfo(
            loadable=True,
            initialized=True,
            device_count=1,
            driver_version="560.35.05",
        ),
        which=_which({}),
        command_runner=FakeRunner({}),
        env={"KUBERNETES_SERVICE_HOST": "10.0.0.1"},
        path_exists=_exists(set()),
        slurm_config_paths=(),
        container_hook_paths=(),
    )

    assert report.plan.mode == "host-systemd"
    assert any("in-cluster environment" in warning for warning in report.plan.warnings)


def test_build_doctor_report_surfaces_docker_permission_denied() -> None:
    runner = FakeRunner(
        {
            ("docker", "info", "--format", "{{json .Runtimes}}"): CommandResult(
                returncode=1,
                stderr="permission denied while trying to connect to the Docker daemon socket",
            )
        }
    )

    report = build_doctor_report(
        dev_paths=[],
        nvml_probe=lambda: NVMLInfo(loadable=True, initialized=False, error="no driver"),
        which=_which(
            {
                "docker": "/usr/bin/docker",
                "nvidia-container-runtime": "/usr/bin/nvidia-container-runtime",
            }
        ),
        command_runner=runner,
        env={},
        path_exists=_exists(set()),
        slurm_config_paths=(),
        container_hook_paths=(),
    )

    container_check = next(check for check in report.checks if check.id == "container_fallback")
    assert container_check.status == "warning"
    assert "docker daemon access required" in container_check.summary
    assert report.plan.mode == "unsupported"


def _which(paths: Mapping[str, str]) -> Callable[[str], str | None]:
    def inner(name: str) -> str | None:
        return paths.get(name)

    return inner


def _exists(paths: set[str]) -> Callable[[str], bool]:
    def inner(path: str) -> bool:
        return path in paths

    return inner
