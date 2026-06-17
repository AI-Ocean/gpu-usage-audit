# 0002 Daemon Cloud Mode (continuous push)

상태: draft
관련: 0001 (enroll + sync-once)
목표: `gua daemon --cloud` — 데몬이 매 틱 local DB 에 기록한 뒤 latest snapshot 을 GUA Board 로 push 한다. 일회성 `sync-once` 를 연속 운영으로 확장(없으면 보드가 stale).

## 배경

0001 에서 enroll + sync-once(1회 수집→local write→push)가 생겼다. 보드가 살아있으려면 호스트가 주기적으로 latest 를 올려야 한다. 기존 `gua daemon` 루프(anti-drift, 시그널 종료, local-write-first)를 재사용해 cloud push 를 얹는다.

## 범위

포함:

- `daemon` 에 `--cloud` + `--config` 플래그(두 CLI 파서 모두: `gua`, `gpu-usage-audit`).
- `daemon.run_daemon`/`_tick` 에 optional `on_tick(snap, ts)` 후크. **daemon 모듈은 cloud 를 import 하지 않는다** — CLI 가 콜백 주입(결합도 분리). 후크는 local write *이후* 호출, 실패해도 로그만 남기고 다음 틱 계속(local-write-first 불변식).
- `_cmd_daemon`: `--cloud` 면 NVML 열기 *전에* `load_cloud_config` 검증(미enroll → exit 2). push 콜백 = `build_observation_payload` + `post_observation`(0001 재사용).
- `gua daemon --cloud`(백그라운드)면 spawn 커맨드에 `--cloud --config` 전파.

제외:

- pull/명령 채널, 재시도 백오프 정교화, 오프라인 큐잉(실패 틱은 다음 틱이 latest 로 덮음 — replay 안 함).
- systemd 유닛 패키징(설치 UX 별도).

## Acceptance

- `gua daemon --cloud`(enrolled): 매 틱 local 기록 + latest push, 보드에 호스트/GPU 표시.
- push 실패(네트워크/CloudError/payload ValueError)는 데몬을 멈추지 않고 local 기록도 보존.
- `--cloud` + 미enroll → exit 2(NVML 열기 전).
- `--cloud` 없으면 기존 동작 그대로(push 없음).
- 백그라운드 `gua daemon --cloud` 가 자식 프로세스로 옵션 전파.

## Verification

- `tests/test_daemon.py`: `on_tick` 매 틱 local write 이후 호출 + raise 해도 데몬 계속·local 보존.
- `tests/test_cloud_cli.py`: `--cloud` 미enroll → exit 2(`run \`gua enroll\``); 백그라운드 spawn 커맨드에 `--cloud/--config` 포함.
- 전체 `pytest` 163 passed, `ruff` clean.

## Implementation Notes

- on_tick 후크 타입 `OnTick = Callable[[Snapshot, datetime], None]` (daemon.py).
- 실패 처리: `_tick` 이 on_tick 을 try/except 로 감싸 `logger.exception` 후 계속(틱 자체는 성공으로 간주).
- "push latest only, no replay" — 실패 틱을 재전송하지 않고 다음 틱 latest 가 보드를 갱신.
