# 0001 GUA Board Cloud Sync (enroll + sync-once)

상태: implemented (branch feat/gua-board-cloud-sync, PR review 대기)
관련 시나리오: GUA Board S1 Host Enrollment, S4 Local Collector Sync, S5 Freshness And Operational Health
목표: 기존 `gpu-usage-audit`(local-only NVIDIA GPU audit CLI)의 수집/저장/report를 유지한 채, GUA Board SaaS에 연결할 수 있는 optional cloud sync 기능(`gua enroll`, `gua sync-once`)을 추가한다.

## 배경

`gpu-usage-audit`는 단일 NVIDIA host에서 NVML telemetry를 SQLite(`~/.gua/gua.db`)에 append하고 `gua report`로 회고 리포트를 출력하는 local-first 오픈소스 도구다. GUA Board는 이 도구의 local-first 철학을 유지하면서, 여러 서버의 latest GPU 가용 상태와 예약을 한 화면에서 보여주는 SaaS다.

이 작업은 GUA Board agent 기능의 첫 슬라이스다. local 동작은 그대로 두고, 사용자가 원할 때만 SaaS에 host를 enroll하고 latest snapshot을 push할 수 있게 한다.

소유권 경계(2026-06-17 결정):

- agent-facing HTTP contract는 GUA Board repo가 소유한다. 본 작업은 그 contract를 따르는 client다.
- local agent 구현은 이 repo(`gpu-usage-audit`)가 소유한다. GUA Board repo 안에 collector를 다시 만들지 않는다.
- contract source of truth: GUA Board `docs/work-specs/0003-agent-snapshot-contract.ko.md`,
  `backend/app/schemas/agent_snapshot.py`, `backend/app/schemas/host_enrollment.py`.

## 따르는 contract (요약, GUA Board가 owner)

`POST /agent/v1/enrollments/claim` → 201 (인증 헤더 없음):

```jsonc
// 요청
{ "enrollmentToken": "gua_enroll_…",   // 필수
  "hostname": "server-a",              // 선택 ≤255
  "agentVersion": "1.1.0",             // 선택 ≤64
  "driverVersion": "560.35.05" }       // 선택 ≤64
// 응답
{ "hostId": "…", "displayName": "a6000-01",
  "agentToken": "gua_agent_…", "tokenPrefix": "gua_agent_xx" }
```

`POST /agent/v1/observations` → 202, 헤더 `Authorization: Bearer <agentToken>`,
body = `availability.snapshot.v1` (camelCase, `extra="forbid"`):

- `schemaVersion` == `"availability.snapshot.v1"` 고정
- `collectionStatus` ∈ `{ok, partial, error}`; `partial`/`error`는 `errors` ≥1, `ok`는 `errors` 비어야 함
- gpu: `index≥0`, `memoryTotalMb>0`, `utilPct∈0..100`, `memoryUsedMb≥0` 그리고 `≤ memoryTotalMb`
- process: `pid>0`, `linuxUser` non-empty, `name` non-empty, `memoryUsedMb≥0`
- `host.hostId`는 받지만 권한 판단에 쓰지 않음 (서버가 token으로 host 결정)

## 범위

포함:

- local SQLite schema v2 확장 (additive): `gpu_sample`/`proc_sample`에 richer metric·식별 컬럼,
  device 정체성 테이블 `gpu_device` 추가. 기존 컬럼/테이블/인덱스는 유지.
  metric(시계열)과 device 정체성(name/memory_total)을 분리해 비정규화를 피한다.
  cloud 링크(host_id/agent_version)는 telemetry DB 가 아니라 `cloud.json`에 둔다 — 관심사 분리.
- 기존 1.0.x DB를 in-place로 올리는 idempotent migration (`PRAGMA table_info` 확인 후 `ADD COLUMN`).
- `NVMLTier.collect()`를 enriched로 업그레이드: GPU name/memory total/memory used/temperature/power,
  process name(`/proc/<pid>/comm`)을 추가 수집. `report`/`classify`/`daemon`/`demo`는 무손상.
- `FakeTier`도 enriched 필드를 채워 GPU 없는 환경/테스트 유지.
- optional cloud 서브패키지 `gpu_usage_audit/cloud/`:
  - `config.py`: `CloudConfig` 저장/로드 (`~/.gua/cloud.json`, mode 0600, atomic write)
  - `client.py`: stdlib `urllib`로 `claim_enrollment` + `post_observation`
  - `snapshot.py`: 최신 local 상태 → `availability.snapshot.v1` payload builder
- `gua enroll` command.
- `gua sync-once` command.
- 단위/통합 테스트, CHANGELOG, README cloud sync 섹션.

제외:

- 연속 cloud sync 루프(`gua daemon --cloud` / `gua sync`) — 후속 work spec.
- WebSocket tunnel, SaaS→agent pull/remote command.
- 실패 tick replay queue.
- token revoke/rotation, OS keychain 암호화 저장.
- systemd cloud runbook.
- local `gpu_slot` 테이블과 slot 상태머신(active/missing/changed/unknown) — server가 materialize하는 개념.
  local report/inventory view가 요구하면 별도 work spec으로 도입.
- 신규 런타임 의존성 (stdlib `urllib`만 사용).

## Acceptance

수집/저장 (local 무손상):

- `gua daemon`/`gua report`/`gua demo`는 기존과 동일하게 동작한다 (회귀 테스트 green).
- 1.0.x에서 만든 기존 `~/.gua/gua.db`를 열어도 데이터 손실 없이 v2 컬럼이 추가된다.
- `NVMLTier.collect()`는 기존 util/process memory에 더해 GPU name·memory total·memory used·process name을 수집한다.
- 같은 GPU(UUID)는 `gpu_device`에 한 번만 등록되고 틱마다 `last_seen`이 갱신된다 (name/memory_total은 정규화 저장).
- GPU별 temperature/power 호출이 실패해도 그 GPU 수집이 중단되지 않고 해당 값만 비운다.

enroll:

- `gua enroll --server-url … --enrollment-token …` 성공 시 `~/.gua/cloud.json`에 cloud config가 저장된다.
- config 파일은 owner-only(0600)로 생성된다.
- 기존 config가 있으면 `--force` 없이 실패한다.
- 성공 출력은 display name·host id·token prefix·config path만 보여준다.
- 실패 메시지(HTTP/네트워크/auth)는 enrollment token·agent token 원문을 노출하지 않는다.
- `--driver-version` 미지정 시 NVML probe를 시도하고, GPU 없는 환경에서도 enroll은 가능하다.

sync-once:

- 순서가 항상 collect → local DB write → cloud push다.
- cloud push가 실패해도 local DB write는 롤백되지 않는다.
- push 성공 시 서버는 202를 반환하고, host의 GPU latest 상태가 갱신된다.
- push 실패 시 local write 성공을 명시하는 메시지를 출력하고 non-zero exit를 반환한다.
- `--fake`로 NVML 없이도 payload를 만들어 push 흐름을 검증할 수 있다.
- 빌드된 payload는 GUA Board `AgentSnapshotV1` 검증을 통과한다 (util 0–100, memoryUsed ≤ memoryTotal,
  linuxUser/name non-empty).
- enroll 전(`cloud.json` 없음)에는 명확한 안내와 함께 실패한다.

## Verification Lane

- host fidelity: NVML mock으로 enriched 수집 변환 로직 검증, FakeTier 기반 payload 검증.
- backend integration (cross-repo smoke): GUA Board compose 기동 → workspace+host 생성 → enrollment token
  발급 → `gua enroll` → `gua sync-once --fake` → board read API에 host/GPU/latest 노출 확인.
- unit/service: cloud config(0600/atomic), urllib client(토큰 미노출), payload builder 클램프/검증,
  schema v2 migration idempotency, report 회귀.

## Owner Review Surface

- `src/gpu_usage_audit/model.py` (GPUSample/ProcSample optional 필드)
- `src/gpu_usage_audit/db.py` (v2 schema + migration + write_snapshot 확장)
- `src/gpu_usage_audit/nvml.py` (enriched collect)
- `src/gpu_usage_audit/paths.py` (cloud config 경로)
- `src/gpu_usage_audit/cloud/config.py`
- `src/gpu_usage_audit/cloud/client.py`
- `src/gpu_usage_audit/cloud/snapshot.py`
- `src/gpu_usage_audit/__main__.py` (enroll/sync-once 파서·핸들러)
- `tests/test_db.py`, `tests/test_nvml.py`, `tests/test_cloud_*.py`
- `CHANGELOG.md`, `README.md` / `README.ko.md`
- `docs/work-specs/0001-gua-board-cloud-sync.ko.md`

## Implementation Notes

데이터 흐름 (불변식):

```
collect (enriched)  →  write local DB (gpu_device upsert + gpu_sample/proc_sample append)  →  build payload  →  push
        daemon·sync 공통                                                                          sync-once만
```

`sync-once`는 한 틱을 enriched 수집해 먼저 local DB에 atomic 하게 기록(commit)한 뒤, **그 동일한
in-memory 스냅샷**에서 payload를 만들어 push한다. write 가 commit 된 뒤 build/push 하므로 in-memory
스냅샷은 방금 기록된 latest 와 동일하다 — 별도 DB read-back/join 은 하지 않는다(중복 쿼리 회피).
gpu_device 로의 정규화는 *저장* 경계의 결정이고, payload 의 name/memory_total 은 in-memory GPUSample
필드에서 직접 온다. push(또는 payload build) 실패는 이미 commit 된 local write 를 막거나 되돌리지
않는다. SaaS 에는 latest 만 보내며 과거 tick 을 replay 하지 않는다. NVML probe 실패 시에는 수집 자체가
불가하므로 local write 도 push 도 없이 명확한 에러로 종료한다.

v2 schema (additive — 기존 컬럼/인덱스 유지). 선택 B: device 정체성은 `gpu_device`로 정규화, `gpu_slot`은 보류:

cloud 링크(host_id/agent_version)는 telemetry DB 가 아니라 `cloud.json`에 저장한다.
`gua.db`의 host 테이블은 local-only identity 그대로 둔다.

```sql
-- gpu_sample: time-varying metric만 추가 (정체성은 gpu_device로 분리)
ALTER TABLE gpu_sample  ADD COLUMN gpu_index       INTEGER;
ALTER TABLE gpu_sample  ADD COLUMN memory_used_mb  INTEGER;
ALTER TABLE gpu_sample  ADD COLUMN temperature_c   INTEGER;
ALTER TABLE gpu_sample  ADD COLUMN power_w         INTEGER;

-- proc_sample: process 식별
ALTER TABLE proc_sample ADD COLUMN gpu_index       INTEGER;
ALTER TABLE proc_sample ADD COLUMN process_name    TEXT;

-- gpu_device: device 정체성 (틱마다 upsert, UUID로 중복 제거). local DB는 단일 host라 host_id 불필요.
CREATE TABLE IF NOT EXISTS gpu_device (
    gpu_uuid        TEXT     NOT NULL,
    name            TEXT     NOT NULL,
    memory_total_mb INTEGER  NOT NULL,
    first_seen      DATETIME NOT NULL,
    last_seen       DATETIME NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gpu_device_uuid ON gpu_device(gpu_uuid);
```

Migration 비용 / destructive 변경 근거:

- `ADD COLUMN`은 nullable이라 기존 row를 재작성하지 않는다. `report`는 새 컬럼을 읽지 않으므로 무손상.
- `gpu_device`는 `CREATE TABLE IF NOT EXISTS`로 추가되고 틱마다 upsert(없으면 insert + first/last_seen, 있으면 last_seen/name/memory_total 갱신)한다. 기존 테이블에 영향 없음.
- 기존 `_migrate_schema()`가 이미 `run_id`를 idempotent하게 추가하는 선례가 있어 같은 패턴을 따른다.
- 행동 변화: `gua daemon`이 틱당 NVML 호출·DB row가 약간 커진다. CHANGELOG에 기록하고 `1.0.3 → 1.1.0`(minor)로 올린다.
- pre-v2 NULL latest row를 push 시도하면 검증 실패하나, `sync-once`는 항상 fresh enriched 수집·기록 후 읽으므로
  실제로는 발생하지 않는다. 방어적으로 enriched 필드 누락 시 명확한 에러를 낸다.

NVML 필드 매핑:

- `name` ← `nvmlDeviceGetName` → `gpu_device.name` (정규화 저장)
- `memoryTotalMb` ← `nvmlDeviceGetMemoryInfo().total` → `gpu_device.memory_total_mb` (정규화 저장)
- `memoryUsedMb` ← `nvmlDeviceGetMemoryInfo().used` → `gpu_sample.memory_used_mb` (metric, bytes `//(1024*1024)`)
- `temperatureC` ← `nvmlDeviceGetTemperature(NVML_TEMPERATURE_GPU)` (실패 시 None)
- `powerW` ← `nvmlDeviceGetPowerUsage() // 1000` (실패 시 None)
- process `name` ← `/proc/<pid>/comm` (`identity.py` 패턴 재사용)
- `linuxUser` ← 기존 loginuid 해석, None이면 `"unknown"` (contract non-empty 요구)

`collectionStatus` 매핑:

- payload builder 는 `ok`/`partial`/`error` 셋 다 지원하고 검증한다 (errors 일관성 강제).
- MVP `gua sync-once` 명령은 성공 수집 시 `ok` 만 emit 한다. NVML init 실패 시에는 push 하지 않고
  명확한 에러로 종료한다(데이터 없음).
- 일부 GPU process list 권한 부족(`partial` + `errors:["process_list_unavailable"]`)과
  NVML 실패 heartbeat(`error`) emit 은 후속 slice로 남긴다. builder/테스트는 이미 갖춰져 있다.

config / 통신:

- HTTP는 stdlib `urllib.request` (신규 의존성 0). httpx 도입은 sync 루프 고도화 시 재검토.
- config 위치 `~/.gua/cloud.json` (`paths.py`에 `DEFAULT_CLOUD_CONFIG_PATH` 추가), mode 0600, atomic replace.
- cloud 코드는 `cloud/` 서브패키지에 격리. enroll 안 한 사용자는 기존 동작 그대로.

권장 커밋 분할 (한 PR 안):

```
c1  model+db: dataclass optional 필드 + v2 migration + gpu_device upsert + write_snapshot 확장 + 테스트
c2  nvml: enriched collect + /proc comm + mock pynvml 업데이트 (report 회귀 green)
c3  cloud/: config + client + 테스트
c4  cmd: gua enroll (+ paths.py)
c5  cmd: gua sync-once (--fake, push 실패가 local write 막지 않음)
c6  docs: 이 work spec + CHANGELOG + README cloud sync 섹션
```

후속 work spec 후보: 연속 sync 루프(`gua daemon --cloud`), token revoke/rotation, systemd cloud runbook,
`partial`/`error` collectionStatus emit.

## Verification Result

branch `feat/gua-board-cloud-sync` 기준:

- `uv run pytest`: 156 passed (기존 131 + 신규 25). report/daemon/demo 회귀 모두 green → local 경로 무손상 확인.
- `uv run ruff check src tests`: All checks passed.
- `uv run mypy`: Success, no issues in 34 source files.
- 신규 테스트 커버리지:
  - `tests/test_db.py`: enriched metric write, gpu_device upsert/first_seen 보존, legacy v1.0 DB in-place 마이그레이션.
  - `tests/test_nvml.py`: enriched device 필드 수집, temperature/power 실패 시 None.
  - `tests/test_identity.py`: `/proc/<pid>/comm` 읽기/누락.
  - `tests/test_cloud_config.py`: 저장/로드/0600/force/normalize.
  - `tests/test_cloud_client.py`: claim/observation urlopen 모킹, HTTP/네트워크 에러에 token 미노출.
  - `tests/test_cloud_snapshot.py`: contract payload shape, clamp, FakeTier→payload.
  - `tests/test_cloud_cli.py`: enroll 성공/force/실패, sync-once --fake local-write-first, push 실패 시 local 보존, 미enroll 시 종료.

보류한 검증:

- 실제 GUA Board 백엔드 대상 cross-stack smoke (compose 기동 → workspace+host → enrollment token →
  `gua enroll` → `gua sync-once --fake` → board read API 노출). 별도 환경에서 수행 예정.
- 실 NVIDIA host 에서 enriched NVML 수집 (host fidelity). 개발/CI 는 mock 으로만 검증.
