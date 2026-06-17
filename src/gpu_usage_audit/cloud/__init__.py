"""Optional GUA Board cloud sync.

local-only 수집/저장/report 와 독립적인 *선택* 기능. 이 서브패키지는
host enrollment(token claim)와 latest snapshot push 만 담당한다.

소유권 경계: agent-facing HTTP contract 의 source of truth 는 GUA Board
repo 다. 여기 코드는 그 contract(`/agent/v1/enrollments/claim`,
`/agent/v1/observations`, `availability.snapshot.v1`)를 따르는 client 일 뿐.
"""

from __future__ import annotations

SCHEMA_VERSION = "availability.snapshot.v1"
"""push payload 의 schemaVersion — GUA Board 가 이 값만 허용한다."""
