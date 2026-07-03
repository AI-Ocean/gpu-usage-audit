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
from typing import Any

import pytest

from gpu_usage_audit import __version__
from gpu_usage_audit.__main__ import (
    DISPLAY_COMMAND_ENV,
    _duration,
    _pid_is_managed_daemon,
    _read_proc_cmdline,
    build_gua_parser,
    build_parser,
    gua_main,
    main,
)
from gpu_usage_audit.doctor import DoctorCheck, DoctorPlan, DoctorReport
from gpu_usage_audit.nvml import NVMLNotAvailableError
from gpu_usage_audit.paths import DEFAULT_DB_PATH


def test_version_string_is_nonempty() -> None:
    assert __version__
    assert isinstance(__version__, str)


def test_parser_registers_subcommands() -> None:
    p = build_parser()
    # 알려진 subcommands 모두 등록됐는지.
    for cmd in ("daemon", "report", "demo", "version", "help"):
        ns = p.parse_args([cmd, *_required_args_for(cmd)])
        assert ns.command == cmd


def test_daemon_and_report_default_to_home_gua_db() -> None:
    p = build_parser()
    assert Path.home() / ".gua" / "gua.db" == DEFAULT_DB_PATH
    assert p.parse_args(["daemon"]).db == str(DEFAULT_DB_PATH)
    assert p.parse_args(["report"]).db == str(DEFAULT_DB_PATH)
    assert p.parse_args(["report"]).interval is None


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

    for cmd in ("daemon", "start", "status", "stop", "report", "demo", "version", "help"):
        ns = p.parse_args([cmd])
        assert ns.command == cmd
    assert p.parse_args(["report"]).interval is None


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


def test_daemon_does_not_create_db_when_nvml_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "gua.db"

    class FailingTier:
        def probe(self) -> str:
            raise NVMLNotAvailableError("NVML unavailable")

        def close(self) -> None:
            pass

    monkeypatch.setattr("gpu_usage_audit.__main__.NVMLTier", FailingTier)

    rc = main(["daemon", "--db", str(db_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "NVML unavailable" in captured.err
    assert not db_path.exists()


def test_gua_daemon_background_refuses_existing_db_before_start(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "gua.db"
    db_path.write_text("existing", encoding="utf-8")

    rc = gua_main(
        [
            "daemon",
            "--db",
            str(db_path),
            "--pid-file",
            str(tmp_path / "gua.pid"),
            "--log-file",
            str(tmp_path / "gua.log"),
        ]
    )

    captured = capsys.readouterr()
    assert rc == 2
    assert f"{db_path} already exists" in captured.err
    assert "gua report" in captured.err


def test_gua_daemon_foreground_uses_foreground_daemon_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "gua.db"
    db_path.write_text("existing", encoding="utf-8")

    rc = gua_main(["daemon", "--foreground", "--db", str(db_path)])

    captured = capsys.readouterr()
    assert rc == 2
    assert f"gua daemon --foreground: {db_path} already exists" in captured.err


def test_gua_daemon_background_starts_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pid_file = tmp_path / "gua.pid"
    log_file = tmp_path / "gua.log"
    db_path = tmp_path / "gua.db"
    seen: dict[str, Any] = {}

    class FakeProc:
        pid = 4242

        def poll(self) -> int | None:
            return None

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProc:
        seen["command"] = command
        seen["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr("gpu_usage_audit.__main__.subprocess.Popen", fake_popen)
    monkeypatch.setattr("gpu_usage_audit.__main__.time.sleep", lambda _seconds: None)

    rc = gua_main(
        [
            "daemon",
            "--db",
            str(db_path),
            "--interval",
            "200ms",
            "--pid-file",
            str(pid_file),
            "--log-file",
            str(log_file),
        ]
    )

    captured = capsys.readouterr()
    command = seen["command"]
    kwargs = seen["kwargs"]
    assert rc == 0
    assert pid_file.read_text(encoding="utf-8") == "4242\n"
    assert command[:3] == [sys.executable, "-m", "gpu_usage_audit"]
    assert command[3:] == [
        "daemon",
        "--db",
        str(db_path),
        "--interval",
        "200ms",
    ]
    assert kwargs["env"][DISPLAY_COMMAND_ENV] == "gua daemon --foreground"
    assert kwargs["start_new_session"] is True
    assert "started pid 4242" in captured.out


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


def test_gua_report_refuses_missing_db_without_creating_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "missing.db"

    rc = gua_main(["report", "--db", str(db_path)])
    captured = capsys.readouterr()
    assert rc == 2
    assert f"gua report: {db_path} does not exist" in captured.err
    assert "gua daemon" in captured.err
    assert not db_path.exists()


def test_gua_status_and_stop_are_idempotent_without_pid_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pid_file = tmp_path / "missing.pid"

    assert gua_main(["status", "--pid-file", str(pid_file)]) == 0
    assert "not running" in capsys.readouterr().out

    assert gua_main(["stop", "--pid-file", str(pid_file)]) == 0
    assert "not running" in capsys.readouterr().out


def test_gua_status_removes_live_pid_that_is_not_gua_daemon(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pid_file = tmp_path / "gua.pid"
    pid_file.write_text("4242\n", encoding="utf-8")

    monkeypatch.setattr("gpu_usage_audit.__main__._pid_alive", lambda _pid: True)
    monkeypatch.setattr("gpu_usage_audit.__main__._pid_is_managed_daemon", lambda _pid: False)

    rc = gua_main(["status", "--pid-file", str(pid_file)])

    assert rc == 0
    assert not pid_file.exists()
    out = capsys.readouterr().out
    assert "not running" in out
    assert "belongs to another process" in out


def test_gua_stop_does_not_signal_live_pid_that_is_not_gua_daemon(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pid_file = tmp_path / "gua.pid"
    pid_file.write_text("4242\n", encoding="utf-8")
    kill_calls: list[tuple[int, int]] = []

    monkeypatch.setattr("gpu_usage_audit.__main__._pid_alive", lambda _pid: True)
    monkeypatch.setattr("gpu_usage_audit.__main__._pid_is_managed_daemon", lambda _pid: False)
    monkeypatch.setattr(
        "gpu_usage_audit.__main__.os.kill",
        lambda pid, sig: kill_calls.append((pid, sig)),
    )

    rc = gua_main(["stop", "--pid-file", str(pid_file)])

    assert rc == 0
    assert kill_calls == []
    assert not pid_file.exists()
    out = capsys.readouterr().out
    assert "not running" in out
    assert "belongs to another process" in out


@pytest.mark.parametrize(
    ("argv", "want"),
    [
        ([sys.executable, "-m", "gpu_usage_audit", "daemon", "--db", "/tmp/gua.db"], True),
        ([sys.executable, "-m", "gpu_usage_audit", "report"], False),
        ([sys.executable, "-m", "other_module", "daemon"], False),
        (["gua", "daemon", "--foreground"], False),
        ([sys.executable, "-m"], False),
        ([], False),
    ],
)
def test_pid_is_managed_daemon_matches_background_spawn_shape(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    want: bool,
) -> None:
    monkeypatch.setattr("gpu_usage_audit.__main__._read_proc_cmdline", lambda _pid: argv)
    assert _pid_is_managed_daemon(4242) is want


def test_read_proc_cmdline_returns_empty_for_missing_pid() -> None:
    assert _read_proc_cmdline(999_999_999) == []


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
        plan=DoctorPlan(
            mode="host",
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
    # action report 의 핵심 섹션 등장.
    for section in (
        "GPU 낭비 진단",
        "■ 조치 필요",
        "■ 즉시 가용",
    ):
        assert section in captured.out, f"{section} not in demo output"
    # DB 파일 생성됐는지.
    assert db_path.exists()


def test_gua_demo_command_records_and_prints_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "demo.db"
    rc = gua_main(
        [
            "demo",
            "--db",
            str(db_path),
            "--ticks",
            "1",
            "--interval",
            "10ms",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "GPU 낭비 진단" in captured.out
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
