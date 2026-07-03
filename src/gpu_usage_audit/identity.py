"""PID → 사용자명 해석. `/proc/<pid>/loginuid` 의 systemd 메커니즘.

`loginuid` 는 sshd/login 이 세션 시작 시 박는 *원래 로그인 사용자* 의
UID 다 — `seteuid` 로 root 가 되더라도 추적 가능. NoLoginUID 인 경우
(daemon spawn 등) UINT32_MAX (4294967295) 가 들어감.

설계: `_parse_loginuid` 를 분리해 *순수 함수* 로 테스트, system path
+ pwd 조회는 system_user_lookup 안.
"""

from __future__ import annotations

import pwd
from pathlib import Path

LOGIN_UID_UNSET = 4294967295  # UINT32_MAX, "loginuid 미설정" 의 sentinel.


def system_owner_lookup(pid: int, proc_root: str | Path = "/proc") -> str | None:
    """PID 를 소유한 실제 UNIX 사용자명 — `/proc/<pid>` 디렉토리의 소유 uid.

    loginuid 와 달리 systemd/컨테이너/nohup 등 *로그인 세션 밖* 프로세스도
    실 uid 는 있어서, loginuid 가 미설정인 경우의 소유자 폴백으로 쓴다.
    best-effort: 컨테이너 user-namespace 처럼 host passwd 에 uid 가 없으면
    None (그 경우 report 는 'unknown' 으로 떨어짐 — 감수).

    None 분기: `/proc/<pid>` 없음(PID 사라짐), stat 실패, uid 가 시스템 user 아님.
    """
    path = Path(proc_root) / str(pid)
    try:
        uid = path.stat().st_uid
    except OSError:
        return None
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None


def _parse_loginuid(data: str) -> int | None:
    """loginuid 파일 내용 → uid 정수 or None.

    None 분기: 빈 문자열, 비정수, UNSET sentinel.
    """
    s = data.strip()
    if not s:
        return None
    try:
        uid = int(s)
    except ValueError:
        return None
    if uid == LOGIN_UID_UNSET:
        return None
    return uid


def system_user_lookup(pid: int, proc_root: str | Path = "/proc") -> str | None:
    """실제 /proc 에서 PID 의 사용자명을 해석. 실패는 모두 None 폴백.

    None 분기:
    - /proc/<pid>/loginuid 가 없음 (PID 사라짐)
    - 파일 내용이 비정수 또는 UNSET
    - pwd.getpwuid 가 UID 를 못 찾음 (UID 만 있고 시스템에 user 없음)
    """
    path = Path(proc_root) / str(pid) / "loginuid"
    try:
        data = path.read_text()
    except OSError:
        return None
    uid = _parse_loginuid(data)
    if uid is None:
        return None
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None


def system_process_name_lookup(pid: int, proc_root: str | Path = "/proc") -> str | None:
    """PID 의 프로세스 이름을 `/proc/<pid>/comm` 에서 읽는다. 실패는 None.

    `comm` 은 커널이 들고 있는 thread/process 이름(보통 executable basename,
    최대 15자). full command line 은 dataset path/token 등 민감정보를 담을
    수 있어 *의도적으로* 수집하지 않는다 — cloud snapshot 의 process name 은
    comm 으로 충분하다.

    None 분기: /proc/<pid>/comm 없음(PID 사라짐), 읽기 실패, 빈 내용.
    """
    path = Path(proc_root) / str(pid) / "comm"
    try:
        data = path.read_text()
    except OSError:
        return None
    name = data.strip()
    return name or None
