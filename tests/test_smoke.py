"""CLI scaffold + duration parsing 통합 smoke 테스트.

세부 동작 (분류/렌더/DB) 은 각 모듈 테스트에 위임. 이 파일은 CLI
entry point + argparse 구조 + duration 파서가 *살아있는지* 만 짚는다.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gpu_usage_audit import __version__
from gpu_usage_audit.__main__ import (
    DEFAULT_DB_PATH,
    _duration,
    build_gua_parser,
    build_parser,
    gua_main,
    main,
)
from gpu_usage_audit.doctor import DoctorCheck, DoctorReport
from gpu_usage_audit.model import RuntimePlan


def test_version_string_is_nonempty() -> None:
    assert __version__
    assert isinstance(__version__, str)


def test_parser_registers_subcommands() -> None:
    p = build_parser()
    # 알려진 subcommands 모두 등록됐는지.
    for cmd in ("daemon", "report", "demo", "version", "help"):
        ns = p.parse_args([cmd, *_required_args_for(cmd)])
        assert ns.command == cmd


def test_daemon_and_report_default_to_tmp_gua_db() -> None:
    p = build_parser()
    assert p.parse_args(["daemon"]).db == str(DEFAULT_DB_PATH)
    assert p.parse_args(["report"]).db == str(DEFAULT_DB_PATH)


def test_pyproject_registers_gua_entry_point() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as f:
        project = tomllib.load(f)["project"]
    scripts = project["scripts"]
    assert scripts["gpu-usage-audit"] == "gpu_usage_audit.__main__:main"
    assert scripts["gua"] == "gpu_usage_audit.__main__:gua_main"
    assert "nvidia-ml-py>=12.535" in project["dependencies"]


def test_gua_parser_registers_command_surface() -> None:
    p = build_gua_parser()
    ns = p.parse_args(["doctor", "--json"])
    assert ns.command == "doctor"
    assert ns.json is True

    ns = p.parse_args(["doctor", "--db", "/var/lib/gua/gua.db"])
    assert ns.command == "doctor"
    assert ns.db == "/var/lib/gua/gua.db"


def _required_args_for(cmd: str) -> list[str]:
    # daemon/report/demo 는 --db 옵셔널. version/help 는 추가 인자 없음.
    return []


def test_main_version_command_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["version"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == __version__


def test_main_help_command_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["help"])
    assert rc == 0
    assert "usage:" in capsys.readouterr().out.lower()


def test_main_no_args_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([])
    assert rc == 2
    assert "usage:" in capsys.readouterr().err.lower()


def test_gua_doctor_prints_runtime_plan(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("gpu_usage_audit.__main__.build_doctor_report", _fake_doctor_report)

    rc = gua_main(["doctor"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Scope:" in captured.out
    assert "machine: local" in captured.out
    assert "Host GPU:" in captured.out
    assert "Recommended commands:" in captured.out


def test_gua_doctor_json_prints_machine_readable_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("gpu_usage_audit.__main__.build_doctor_report", _fake_doctor_report)

    rc = gua_main(["doctor", "--json"])
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert rc == 0
    assert data["scope"] == {"machine": "local"}
    assert data["read_only"] is True
    assert data["no_system_changes"] is True
    assert data["plan"]["mode"] == "host"
    assert "No system" not in captured.out


def test_gua_doctor_passes_db_path_to_report_builder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "custom.db"
    seen: dict[str, str | Path] = {}

    def fake_report(*, db_path: str | Path) -> DoctorReport:
        seen["db_path"] = db_path
        return _fake_doctor_report(db_path=db_path)

    monkeypatch.setattr("gpu_usage_audit.__main__.build_doctor_report", fake_report)

    rc = gua_main(["doctor", "--db", str(db_path)])

    assert rc == 0
    assert seen["db_path"] == str(db_path)
    assert f"target: {db_path}" in capsys.readouterr().out


def test_daemon_refuses_existing_db_before_nvml(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "gua.db"
    db_path.write_text("existing", encoding="utf-8")

    rc = main(["daemon", "--db", str(db_path)])
    captured = capsys.readouterr()
    assert rc == 2
    assert f"{db_path} already exists" in captured.err


def test_report_refuses_missing_db_without_creating_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "missing.db"

    rc = main(["report", "--db", str(db_path)])
    captured = capsys.readouterr()
    assert rc == 2
    assert f"{db_path} does not exist" in captured.err
    assert not db_path.exists()


def _fake_doctor_report(*, db_path: str | Path = DEFAULT_DB_PATH) -> DoctorReport:
    return DoctorReport(
        generated_at=datetime(2026, 5, 14, 0, 0, tzinfo=UTC),
        checks=[
            DoctorCheck(
                id="os",
                name="OS/kernel/Python",
                status="ok",
                summary="Linux 6.0, Python 3.12.0",
            ),
            DoctorCheck(
                id="nvidia_devices",
                name="/dev/nvidia*",
                status="ok",
                summary="2 GPU device files",
            ),
            DoctorCheck(
                id="nvidia_smi",
                name="nvidia-smi",
                status="ok",
                summary="2 GPUs",
            ),
            DoctorCheck(
                id="nvml",
                name="NVML",
                status="ok",
                summary="initialized, GPU count=2, driver 560.35.05",
            ),
            DoctorCheck(
                id="default_db",
                name="default DB path",
                status="ok",
                summary="absent, ready for a new daemon run",
                details={"path": str(db_path), "is_default": Path(db_path) == DEFAULT_DB_PATH},
            ),
        ],
        plan=RuntimePlan(
            mode="host",
            telemetry="nvml",
            scheduler="none",
            confidence="high",
            reasons=["Local NVML initialized and sees 2 GPU(s)."],
        ),
    )


def test_cli_entry_point_runs_in_subprocess() -> None:
    # installed entry point `gpu-usage-audit` 와 `python -m` 둘 다 동일 main().
    result = subprocess.run(
        [sys.executable, "-m", "gpu_usage_audit", "version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == __version__


def test_demo_command_records_and_prints_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # demo: 작은 ticks 로 self-contained 시연. stdout 에 §1~§5 등장,
    # DB 에 sample 적재 확인.
    db_path = tmp_path / "demo.db"
    rc = main(
        [
            "demo",
            "--db",
            str(db_path),
            "--ticks",
            "3",
            "--interval",
            "10ms",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    # §1~§5 다 등장.
    for section in ("§1 Headline", "§2 Waste", "§3 Per-GPU", "§4 Top identities", "§5"):
        assert section in captured.out, f"{section} not in demo output"
    # DB 파일 생성됐는지.
    assert db_path.exists()


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("30s", timedelta(seconds=30)),
        ("1h", timedelta(hours=1)),
        ("200ms", timedelta(milliseconds=200)),
        ("0.5m", timedelta(seconds=30)),
        ("2d", timedelta(days=2)),
        # 상한 없음 — 365d 도 OK.
        ("365d", timedelta(days=365)),
    ],
)
def test_duration_parser_valid(text: str, want: timedelta) -> None:
    assert _duration(text) == want


@pytest.mark.parametrize("bad", ["30", "h", "1y", "1.5", "1 s", "", "1w"])
def test_duration_parser_invalid(bad: str) -> None:
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        _duration(bad)
