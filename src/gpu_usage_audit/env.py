"""호스트 환경 분류 — `/proc/1/cgroup` 의 마지막 필드를 보고 bare/docker/k8s 결정.

PID 1 은 부팅 직후 커널이 띄우는 init — bare 머신이면 systemd 관리
경로(`/system.slice/...`, `/init.scope` 등), 컨테이너 안이면
`/docker/...` 또는 `/kubepods/...` 같은 시그니처가 등장한다.

매칭 우선순위: k8s → docker → bare → unknown.
- k8s 를 먼저 보는 이유: k8s 파드는 내부적으로 docker/containerd 위에
  도는 경우가 흔해 docker 시그니처가 false positive 가 될 수 있다.
- unknown 은 silent 폴백 — *알 수 없는 환경* 을 "bare 인 척" 하면 위험.
"""

from __future__ import annotations

from pathlib import Path


def detect_env_kind(proc_root: str | Path = "/proc") -> str:
    """`proc_root/1/cgroup` 을 읽고 "bare"/"docker"/"k8s"/"unknown" 반환.

    Args:
        proc_root: 일반적으로 `/proc`. 테스트에서는 t.TempDir() 같은
            pyfakefs 대신 *실 파일* 픽스처를 깔아도 동작 — Go 의
            DetectEnvKind 와 동일한 시그니처.

    Returns:
        분류 문자열. 파일 부재/읽기 실패 시 "unknown".
    """
    path = Path(proc_root) / "1" / "cgroup"
    try:
        data = path.read_text()
    except OSError:
        return "unknown"

    if "kubepods" in data:
        return "k8s"
    if "docker" in data or "containerd" in data:
        return "docker"

    # cgroup 라인 형식: "<hierarchy>:<controllers>:<path>" (v1) 또는
    # "0::<path>" (v2). 마지막 필드가 systemd 관리 경로면 bare.
    for raw_line in data.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        p = parts[2]
        if (
            p == "/"
            or p == "/init.scope"
            or p.startswith("/system.slice")
            or p.startswith("/user.slice")
        ):
            return "bare"
    return "unknown"
