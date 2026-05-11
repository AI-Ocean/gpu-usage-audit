"""DetectEnvKind 테스트. Go v0.1.0 의 TestDetectEnvKind 와 동일 케이스."""

from __future__ import annotations

from pathlib import Path

import pytest

from gpu_usage_audit.env import detect_env_kind


@pytest.mark.parametrize(
    ("name", "content", "want"),
    [
        (
            "k8s — kubepods 경로",
            "12:devices:/kubepods/besteffort/pod-abc/container-xyz\n",
            "k8s",
        ),
        (
            "k8s 우선순위 — kubepods + docker 둘 다",
            "12:devices:/kubepods/...\n11:cpu:/docker/abc\n",
            "k8s",
        ),
        ("docker — docker 경로", "12:devices:/docker/abcdef\n", "docker"),
        ("docker — containerd 경로", "12:devices:/containerd/xyz\n", "docker"),
        ("bare — system.slice", "0::/system.slice/gpu-audit.service\n", "bare"),
        ("bare — init.scope", "0::/init.scope\n", "bare"),
        ("bare — 루트 경로", "0::/\n", "bare"),
        ("bare — user.slice", "0::/user.slice/user-1000.slice\n", "bare"),
        ("unknown — 모르는 경로", "0::/some/weird/path\n", "unknown"),
    ],
)
def test_detect_env_kind_from_content(
    tmp_path: Path,
    name: str,
    content: str,
    want: str,
) -> None:
    proc_dir = tmp_path / "1"
    proc_dir.mkdir()
    (proc_dir / "cgroup").write_text(content)
    got = detect_env_kind(tmp_path)
    assert got == want, f"{name}: got {got!r}, want {want!r}\n  content={content!r}"


def test_detect_env_kind_missing_file(tmp_path: Path) -> None:
    # proc_root 자체는 존재하지만 1/cgroup 파일 없음 — unknown 폴백.
    assert detect_env_kind(tmp_path) == "unknown"


def test_detect_env_kind_missing_root(tmp_path: Path) -> None:
    # proc_root 자체가 없는 경로도 OSError 흡수 → unknown.
    nonexistent = tmp_path / "does-not-exist"
    assert detect_env_kind(nonexistent) == "unknown"
