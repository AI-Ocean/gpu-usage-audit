# 0003 Collection Status Emit (partial / error)

상태: implemented
관련: 0001 (enroll + sync-once, payload builder), 0002 (daemon cloud mode)
목표: agent 가 수집이 실제로 저하/실패했을 때 `collectionStatus` 를 `partial`/`error` 로 emit 한다. 지금까지 두 push call site 는 항상 `ok` 만 보냈다. board 측은 partial/error 를 이미 검증·저장하므로 board 변경은 없다 (agent 전용).

## 배경

0001 의 payload builder(`build_observation_payload`)는 `ok`/`partial`/`error` 셋 다 지원·검증한다 (`partial`/`error` 는 errors ≥1, `ok` 는 errors 비어야 함). 하지만 MVP 는 후속으로 미뤄 두 call site(`sync-once`, daemon `push_snapshot`)가 status/errors 를 생략해 항상 `ok` 였다 (0001 Implementation Notes "collectionStatus 매핑").

실제 운영에서 수집은 두 가지로 저하된다:
- 일부 카드의 process list 가 권한/일시오류로 안 읽힌다 — core GPU metric 은 정상. board 가 idle-held 신호를 *과소평가* 할 수 있으니 `partial` 로 알려야 한다.
- NVML init 자체가 실패한다(드라이버 손실 등) — GPU inventory 가 아예 없다. crash 로 끝내는 대신 `error` heartbeat 를 보내 board 가 non-ok freshness 로 표시하게 한다.

## 범위

포함:

- `NVMLTier.collect()` 가 그 틱에 process list 를 못 읽은 카드를 *틱마다 리셋* 되는 집합으로 추적하고 `last_process_list_unavailable: bool` 로 노출. 기존 누적 warning 집합(반복 로그 억제)과 분리.
- `cloud.snapshot.derive_collection_status(snapshot, *, process_list_unavailable)` — 수집 결과 → `(status, errors)`. GPU 가 있고 process list 가 비었으면 `partial` + `["process_list_unavailable"]`, 그 외 `ok`. contract 와 같은 곳에 둬 두 call site 가 같은 규칙 공유.
- 안정적 short error code 상수: `process_list_unavailable`, `nvml_init_failed`.
- `sync-once`: collect 후 partial 신호를 builder 로 thread. NVML init 실패 시 `error` heartbeat(빈 inventory, `nvml_init_failed`) push 후 non-zero exit. 데이터가 없으므로 local DB 는 쓰지 않는다.
- daemon `push_snapshot`: 매 틱 `tier.last_process_list_unavailable` 로 partial 도출해 thread (on_tick 은 같은 스레드에서 collect 직후 동기 호출이라 일치).

제외:

- daemon 의 `error` heartbeat. daemon 은 NVML 열기 *전에* probe 실패면 즉시 종료하는 구조라(0002 `_cmd_daemon`), error heartbeat 를 끼우려면 cloud config 검증 순서·종료 경로를 재배치해야 한다. 위험 대비 가치가 낮아 보류 — sync-once 의 error heartbeat 로 단발 진단은 가능하다.
- 새 error code 분류(예: GPU별 temperature/power 실패) — builder 는 음수 sentinel 을 0 으로 눌러 이미 흡수하므로 partial 로 격상하지 않는다.
- 신규 의존성/version bump/release tag (별도 수동 단계).

## Acceptance

- 모든 카드 정상 수집 → `ok`, errors 빈 배열 (기존과 동일).
- 한 카드라도 process list 가 NVMLError 로 비고 core GPU metric 은 수집됨 → `partial` + `["process_list_unavailable"]`, GPU 데이터는 그대로 push.
- partial 은 *틱 단위* — 다음 틱에 process list 가 복구되면 `ok` 로 돌아온다.
- NVML init 실패(`sync-once`) → `error` + `["nvml_init_failed"]` heartbeat push, GPU 빈 배열, local write 없음, non-zero exit. heartbeat push 도 실패하면 두 에러를 모두 보고.
- 두 call site 가 도출한 status/errors 를 builder 에 넘기며, builder 검증을 그대로 통과(partial/error errors ≥1).

## Verification

- `tests/test_nvml.py`: `last_process_list_unavailable` 가 process list 실패 틱에 True, 복구 틱에 False 로 리셋(틱 단위), core GPU metric 유지.
- `tests/test_cloud_snapshot.py`: `derive_collection_status` ok/partial 도출, GPU 0개면 partial flag 무시(ok), partial 도출값이 builder 검증 통과.
- `tests/test_cloud_cli.py`: `sync-once` 가 process list 불가 시 `partial` payload + GPU 보존 + local write; NVML init 실패 시 `error` heartbeat(빈 inventory, local write 없음, exit 1); heartbeat push 실패 시 두 에러 모두 보고.
- 전체 `uv run pytest`: 170 passed (기존 163 + 신규 7). `ruff check` clean, `mypy` clean.

## Implementation Notes

- partial 판정은 `derive_collection_status` 한곳 — `sync-once`(`NVMLTier.last_process_list_unavailable`) 와 daemon(같은 tier 인스턴스 closure) 이 공유.
- error heartbeat 는 `Snapshot()`(빈) 으로 builder 호출 — GPU 0개라 검증 통과하고, errors 비우지 않아 `error` contract 충족. driver_version 은 probe 실패라 `"unknown"`.
- daemon error heartbeat 는 보류 — 위 "제외" 참조. partial 경로는 daemon·sync-once 양쪽 모두 적용.
