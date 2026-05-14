"""Local bare-metal doctor readiness tests."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

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


def test_build_doctor_report_checks_only_local_bare_metal(tmp_path: Path) -> None:
    runner = FakeRunner(
        {
            ("nvidia-smi", "-L"): CommandResult(
                returncode=0,
                stdout=(
                    "GPU 0: NVIDIA A100-SXM4-40GB (UUID: GPU-a)\n"
                    "GPU 1: NVIDIA A100-SXM4-40GB (UUID: GPU-b)\n"
                ),
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
        which=_which({"nvidia-smi": "/usr/bin/nvidia-smi"}),
        command_runner=runner,
        db_path=tmp_path / "gua.db",
    )

    assert [check.id for check in report.checks] == [
        "os",
        "nvidia_devices",
        "nvidia_smi",
        "nvml",
        "default_db",
    ]
    assert runner.calls == [("nvidia-smi", "-L")]
    assert report.plan.mode == "host"
    assert report.plan.telemetry == "nvml"
    assert report.plan.scheduler == "none"

    rendered = render_doctor(report)
    assert "Scope:\n  machine: local" in rendered
    assert "Host GPU:" in rendered
    assert "nvidia-smi: ok, 2 GPUs" in rendered
    assert "NVML: ok, initialized, GPU count=2, driver 560.35.05" in rendered
    assert "status: absent, ready for a new daemon run" in rendered
    assert "Recommended commands:" in rendered
    assert "Kubernetes" not in rendered
    assert "Slurm" not in rendered
    assert "Docker" not in rendered


def test_build_doctor_report_marks_unsupported_when_nvml_is_missing(tmp_path: Path) -> None:
    report = build_doctor_report(
        dev_paths=["/dev/nvidia0", "/dev/nvidiactl"],
        nvml_probe=lambda: NVMLInfo(
            loadable=False,
            initialized=False,
            error="pynvml is not importable\nreinstall tool",
        ),
        which=_which({"nvidia-smi": "/usr/bin/nvidia-smi"}),
        command_runner=FakeRunner(
            {
                ("nvidia-smi", "-L"): CommandResult(
                    returncode=0,
                    stdout="GPU 0: NVIDIA A100-SXM4-40GB (UUID: GPU-a)\n",
                )
            }
        ),
        db_path=tmp_path / "gua.db",
    )

    assert report.plan.mode == "unsupported"
    assert any("NVML could not be loaded" in blocker for blocker in report.plan.blockers)
    rendered = render_doctor(report)
    assert "NVML: error, pynvml is not importable" in rendered
    assert "Fix:" in rendered
    assert "uv tool install --force gpu-usage-audit" in rendered
    assert "Recommended commands:" not in rendered


def test_build_doctor_report_guides_driver_repair_when_nvml_init_fails(tmp_path: Path) -> None:
    report = build_doctor_report(
        dev_paths=["/dev/nvidia0", "/dev/nvidiactl"],
        nvml_probe=lambda: NVMLInfo(
            loadable=True,
            initialized=False,
            error=(
                "NVML initialization failed: the NVIDIA driver and NVML library versions "
                "do not match. Detail: Driver/library version mismatch"
            ),
        ),
        which=_which({"nvidia-smi": "/usr/bin/nvidia-smi"}),
        command_runner=FakeRunner(
            {
                ("nvidia-smi", "-L"): CommandResult(
                    returncode=0,
                    stdout="GPU 0: NVIDIA A100-SXM4-40GB (UUID: GPU-a)\n",
                )
            }
        ),
        db_path=tmp_path / "gua.db",
    )

    rendered = render_doctor(report)
    assert report.plan.mode == "unsupported"
    assert "NVML: error, loadable but init failed" in rendered
    assert "Install or repair the NVIDIA driver" in rendered
    assert "libnvidia-ml.so.1" in rendered
    assert "uv tool install --force --with nvidia-ml-py" not in rendered


def test_default_db_present_warns_without_blocking_host_plan(tmp_path: Path) -> None:
    db_path = tmp_path / "gua.db"
    db_path.write_text("existing", encoding="utf-8")

    report = build_doctor_report(
        dev_paths=["/dev/nvidia0", "/dev/nvidiactl"],
        nvml_probe=lambda: NVMLInfo(
            loadable=True,
            initialized=True,
            device_count=1,
            driver_version="560.35.05",
        ),
        which=_which({"nvidia-smi": "/usr/bin/nvidia-smi"}),
        command_runner=FakeRunner(
            {
                ("nvidia-smi", "-L"): CommandResult(
                    returncode=0,
                    stdout="GPU 0: NVIDIA A100-SXM4-40GB (UUID: GPU-a)\n",
                )
            }
        ),
        db_path=db_path,
    )

    db_check = next(check for check in report.checks if check.id == "default_db")
    assert report.plan.mode == "host"
    assert db_check.status == "warning"
    assert any("already exists" in warning for warning in report.plan.warnings)
    rendered = render_doctor(report)
    assert "daemon will refuse" in rendered
    assert "collect:" not in rendered
    assert "report existing data:" in rendered


def test_custom_db_path_is_rendered_and_shell_quoted(tmp_path: Path) -> None:
    db_path = tmp_path / "with space" / "gua db.sqlite"
    db_path.parent.mkdir()

    report = build_doctor_report(
        dev_paths=["/dev/nvidia0", "/dev/nvidiactl"],
        nvml_probe=lambda: NVMLInfo(
            loadable=True,
            initialized=True,
            device_count=1,
            driver_version="560.35.05",
        ),
        which=_which({"nvidia-smi": "/usr/bin/nvidia-smi"}),
        command_runner=FakeRunner(
            {
                ("nvidia-smi", "-L"): CommandResult(
                    returncode=0,
                    stdout="GPU 0: NVIDIA A100-SXM4-40GB (UUID: GPU-a)\n",
                )
            }
        ),
        db_path=db_path,
    )

    rendered = render_doctor(report)
    quoted = f"'{db_path}'"
    assert f"target: {db_path}" in rendered
    assert f"collect: gpu-usage-audit daemon --db {quoted} --interval 30s" in rendered
    assert (
        f"report after collecting: gpu-usage-audit report --db {quoted} --since 1h --interval 30s"
    ) in rendered


def test_nvidia_smi_counts_mig_instances(tmp_path: Path) -> None:
    report = build_doctor_report(
        dev_paths=["/dev/nvidia0", "/dev/nvidiactl"],
        nvml_probe=lambda: NVMLInfo(
            loadable=True,
            initialized=True,
            device_count=1,
            driver_version="560.35.05",
        ),
        which=_which({"nvidia-smi": "/usr/bin/nvidia-smi"}),
        command_runner=FakeRunner(
            {
                ("nvidia-smi", "-L"): CommandResult(
                    returncode=0,
                    stdout=(
                        "GPU 0: NVIDIA A100-SXM4-40GB (UUID: GPU-a)\n"
                        "  MIG 1g.5gb Device 0: (UUID: MIG-a)\n"
                    ),
                )
            }
        ),
        db_path=tmp_path / "gua.db",
    )

    smi_check = next(check for check in report.checks if check.id == "nvidia_smi")
    assert smi_check.status == "ok"
    assert smi_check.summary == "1 GPU, 1 MIG instance"


def test_doctor_report_json_is_local_scope(tmp_path: Path) -> None:
    report = build_doctor_report(
        dev_paths=["/dev/nvidia0", "/dev/nvidiactl"],
        nvml_probe=lambda: NVMLInfo(
            loadable=True,
            initialized=True,
            device_count=1,
            driver_version="560.35.05",
        ),
        which=_which({"nvidia-smi": "/usr/bin/nvidia-smi"}),
        command_runner=FakeRunner(
            {
                ("nvidia-smi", "-L"): CommandResult(
                    returncode=0,
                    stdout="GPU 0: NVIDIA A100-SXM4-40GB (UUID: GPU-a)\n",
                )
            }
        ),
        db_path=tmp_path / "gua.db",
    )

    data = doctor_report_to_dict(report)
    assert data["scope"] == {"machine": "local"}
    assert data["read_only"] is True
    assert data["no_system_changes"] is True
    plan = data["plan"]
    checks = data["checks"]
    assert isinstance(plan, dict)
    assert isinstance(checks, list)
    assert plan["mode"] == "host"
    assert plan["actions"] == []
    assert [check["id"] for check in checks if isinstance(check, dict)] == [
        "os",
        "nvidia_devices",
        "nvidia_smi",
        "nvml",
        "default_db",
    ]
    json.dumps(data)


def _which(paths: Mapping[str, str]) -> Callable[[str], str | None]:
    def inner(name: str) -> str | None:
        return paths.get(name)

    return inner
