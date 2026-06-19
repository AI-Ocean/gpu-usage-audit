# 0004 NVML-only GPU usage classification (compute/graphics split + idle-held over time)

상태: draft
목표: GPU 를 **NVML 만으로** `active` / `idle-held` / `idle` 로 정확히 분류한다. 외부 시스템 도구(DCGM, nvidia-smi 등)는 도입하지 않고, `nvidia-ml-py`(드라이버 번들 `libnvidia-ml.so`)의 **무권한 호출만** 쓴다. 핵심은 (1) compute + graphics 프로세스를 둘 다 열거해 타입 태깅, (2) 메모리는 프로세스별 `usedGpuMemory` 로 귀속(총 메모리는 정보로만), (3) 상태는 **residency + 시간창 util** 로 분류한다.

## 배경

실 호스트(ds4, RTX A6000 ×10)에서 모든 GPU 가 idle 인데 보드가 "사용 중"으로 떴다. `nvidia-smi` 확인 결과 각 GPU 에 `Xorg`(graphics, Type G) 만 점유, compute(Type C) 프로세스는 0. 원인 + 정밀 탐지법을 deep-research 로 조사(아래 근거)했고, 결론은:

- **NVML 은 프로세스를 compute / graphics 컨텍스트로 분리**한다. `nvmlDeviceGetComputeRunningProcesses` 는 graphics 앱(Xorg/OpenGL)을 *제외*하고, 그것들은 `nvmlDeviceGetGraphicsRunningProcesses` 에 있다. 현재 agent 는 compute 목록만 봐서 Xorg 점유를 못 보고, 보드는 **총 메모리**(`nvmlDeviceGetMemoryInfo`, graphics 포함)로 in-use 를 쳐서 false positive 가 났다.
- **메모리 귀속의 1차 단위는 프로세스별 `usedGpuMemory`** 이지 device-total 임계가 아니다.
- **`usedGpuMemory` 가 N/A(None)여도 프로세스는 실재**한다(WDDM/MIG/권한 등 "보고 불가"일 뿐). 현재 agent 의 `if used is None: continue`(skip)는 결함 — 사각지대를 만든다.
- **NVML util 은 시간 기반 duty-cycle**(메모리-only 커널도 100% 가능, 단일 SM 도 100%) — 비대칭이다. 높은 util 은 "유용한 일"을 증명 못 하지만 **지속 ~0% util 은 "커널 미실행"의 신뢰 신호**다.
- **idle-held vs truly-idle 의 구분선은 메모리가 아니라 "프로세스 residency"** 다. 3-state(deep-idle / execution-idle=idle-held / active)로 보는 게 정확하다.

## 범위 (포함)

- **compute + graphics 둘 다 열거 + 타입 태깅**: 매 tick 에서 `nvmlDeviceGetComputeRunningProcesses` 와 `nvmlDeviceGetGraphicsRunningProcesses` 를 모두 호출, 각 프로세스에 `process_type`(`compute` | `graphics`) 부여. 같은 pid 가 양쪽에 있으면(C+G) `compute` 우선.
- **`usedGpuMemory=None` 프로세스 유지**: skip 중단. `mem_used_mb` 를 `int | None` 로 바꿔 "memory-unknown" 로 보고(프로세스는 살아 있으므로 residency 에 포함).
- **per-GPU `usage_state` 시간창 분류**: `active` / `idle_held` / `idle` 를 NVML util 시계열 + compute residency 로 계산해 GPU 마다 emit.
  - `idle`(=deep idle, truly idle): compute 프로세스 residency 없음(graphics-only/Xorg 포함). → "사용 중" 아님.
  - `idle_held`(=execution-idle): compute 프로세스 residency 있음 AND util 이 임계 이하로 윈도우 동안 지속. → GUA 의 핵심 타깃.
  - `active`: compute 프로세스 residency 있음 AND 최근 util 이 임계 초과.
- **snapshot 페이로드 계약 additions**(board 협응 필요): GPU 객체에 `usageState`; process 객체에 `type`; process `memoryUsedMb` 를 **nullable** 로(memory-unknown).
- agent 단위 테스트: fake NVML tier 확장(graphics 프로세스, None 메모리, util 시계열), state classifier 단위 테스트.

## 범위 (제외 — Non-goals)

- **DCGM / dcgm-exporter**: 별도 시스템 패키지(`datacenter-gpu-manager`) + 권한 `nv-hostengine` 데몬 + 프로파일링은 root 필요 → 자족 `uv tool install`(nvidia-ml-py 만) 모델을 깬다. **도입하지 않음.** (사이트가 *이미* DCGM/dcgm-exporter 를 돌리는 경우 "있으면 읽는" 선택적 외부 연동은 먼 future 후보로만 남긴다 — agent 가 설치·요구하지 않는다.)
- **nvidia-smi 등 외부 프로세스 파싱**: NVML 바인딩이 단일 데이터 소스. CLI shell-out 안 함.
- **NVML accounting mode**(`nvmlDeviceSetAccountingMode`/`GetAccountingStats`): 켜려면 root, 드라이버 언로드 시 리셋, 기본 비활성 → 의존하지 않음.
- per-PID 정밀 utilization, MPS 경합 귀속, MIG 전용 처리 — NVML 정확도 한계/환경 한정이라 제외.
- **컨테이너 *내부* 실행**: NVML 은 PID namespace 와 비호환(컨테이너 안에서 host PID 가 안 풀림) → agent 는 **호스트 실행** 전제(현 ds4 가 그러함). 컨테이너↔cgroup 귀속은 별도 후속.
- **board 스키마/UI 변경**: 별도 gua-board work-spec 으로 협응(계약 additions 수용 + idle_held/graphics 표시). 현재 board 의 메모리 임계(gua-board #22 의 2GiB band-aid)는 **agent 의 `usage_state` 로 대체**하는 게 board 쪽 목표 — 본 스펙은 신호 생산(agent)까지.

## Acceptance

- graphics-only GPU(Xorg 만, compute residency 0, util 0) → `usage_state=idle`(사용 중 아님). [보고된 버그 해소]
- compute 프로세스 residency + util 이 윈도우 동안 지속 ~0 → `usage_state=idle_held`.
- compute 프로세스 residency + 최근 util 임계 초과 → `usage_state=active`.
- `usedGpuMemory=None` 프로세스가 **드롭되지 않고** 보고된다(`memoryUsedMb=null`, `type` 태깅, residency 에 포함).
- graphics 프로세스가 `type=graphics` 로 보고되고 compute "사용 중" 으로 집계되지 않는다.
- **새 시스템 의존성 0** — 여전히 `uv tool install gpu-usage-audit` 자족(nvidia-ml-py 만), root 불필요.
- 기존 collectionStatus(0003)/cloud sync/`gua report` 회귀 없음.

## Verification Lane

- agent: `uv run pytest`(fake NVML tier 로 graphics 프로세스·None 메모리·util 시계열 시나리오), state classifier 단위 테스트, ruff/format/mypy clean.
- cross-stack: 새 페이로드 필드를 board 가 수용하는지(별도 board work-spec 머지 후) `--fake` smoke.

## Implementation Notes

- **NVML 호출(전부 무권한, 드라이버 번들)**: `nvmlDeviceGetComputeRunningProcesses` + `nvmlDeviceGetGraphicsRunningProcesses`, `nvmlDeviceGetUtilizationRates`, `nvmlDeviceGetMemoryInfo`. C+G 중복 pid dedup(compute 우선). graphics 목록 호출도 권한 부족 시 NVMLError 가능 → compute 와 동일하게 그 카드만 비우고 collectionStatus partial 반영(0003 패턴 재사용).
- **residency**: 해당 GPU 에 `process_type=compute` 프로세스가 ≥1 이면 resident. memory-unknown(None) 도 resident 로 친다.
- **시간창 분류**: `idle_held` = resident AND util ≤ `U_idle` 가 ≥ `W` 동안 연속. `active` = resident AND 최근 util > `U_idle`. daemon(연속)은 최근 틱들(로컬 SQLite history 또는 in-memory ring)로 윈도우 평가. **`sync-once`(단발)는 윈도우가 없으므로** residency + 그 틱 util 로 단일-틱 근사(문서화).
- **기본값(튜닝 가능)**: `U_idle≈5%`, `W` 는 틱 간격(daemon 30s) 고려해 "최근 N틱 연속 저활동"(예: 수 분). 참고 클러스터 규칙은 <5% / ≥5s / 1Hz(arXiv 2604.04745) 이나 GUA daemon 틱(30s)에 맞춰 조정.
- **util 한계 수용**: SM 단위 정밀도(DCGM)는 non-goal. NVML device util 은 비대칭이나 우리 용도(idle_held=util~0)엔 안전. memory-only 커널이 util 100% 로 보이는 케이스는 `active` 로 분류돼도 무방(보수적).
- **model/payload**: `ProcSample.mem_used_mb: int | None`, `ProcSample.process_type: str`; `GPUSample`(또는 빌드 단계)에서 `usage_state` 산출. `cloud/snapshot._build_processes` 에서 `type`/nullable `memoryUsedMb` emit, GPU dict 에 `usageState`. board `agent_snapshot` schema 가 이를 수용해야 함(SnapshotProcess.memory_used_mb nullable, process.type, gpu.usageState) — 협응 항목.

## 근거 (deep-research, 2026-06-18)

NVIDIA 공식 docs + CMU arXiv 중심으로 검증(3표 적대적):
- NVML compute/graphics 프로세스 분리, `usedGpuMemory`= per-process 귀속, util= 시간 duty-cycle: NVML Device Queries / nvmlProcessInfo_t / nvmlUtilization_t (`docs.nvidia.com/deploy/nvml-api/...`), nvidia-smi 레퍼런스.
- 3-state(deep-idle / execution-idle / active), residency 가 구분선, <5%/≥5s/1Hz 규칙: arXiv:2604.04745 "The Energy Cost of Execution-Idle in GPU Clusters".
- DCGM 프로파일링은 admin/superuser nv-hostengine 필요(drop-in 아님): DCGM Feature Overview.
- 컨테이너: NVML×PID namespace 비호환 → 호스트 실행 권고: nvidia-container-toolkit FAQ.
