# gpu-usage-audit

로컬 NVIDIA 호스트에서 GPU 사용 기록을 수집하고, **util은 낮지만 메모리를 잡고 있어 못 쓰는 GPU**를 `idle-held`로 따로 보여주는 감사 도구입니다.

[![PyPI](https://img.shields.io/pypi/v/gpu-usage-audit.svg)](https://pypi.org/project/gpu-usage-audit/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://pypi.org/project/gpu-usage-audit/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![GitHub Release](https://img.shields.io/github/v/release/AI-Ocean/gpu-usage-audit)](https://github.com/AI-Ocean/gpu-usage-audit/releases)

[English](README.md) · [한국어](README.ko.md) · [Releases](https://github.com/AI-Ocean/gpu-usage-audit/releases) · [Issues](https://github.com/AI-Ocean/gpu-usage-audit/issues)

---

![gua top — 로컬 라이브 GPU 뷰](https://raw.githubusercontent.com/AI-Ocean/gpu-usage-audit/main/docs/img/gua-top.png)

*`gua top` — 보드나 웹 없이 터미널에서 바로 보는 라이브 뷰. GPU0은 실제로 바쁘고, GPU1은 util 3%인데 8.2 GB를 잡고 있고(**idle-held**), GPU2는 진짜로 비어 있습니다.*

## 소개

gpu-usage-audit는 다른 dashboard가 놓치는 질문 하나에 답합니다 — **이 GPU가 진짜 비어 있나, 아니면 메모리만 잡은 채 놀고 있나?** 로컬 NVIDIA/NVML telemetry를 보고 GPU card-tick을 다음 세 상태로 나눕니다.

- `active`: 실제 연산이 일어나는 상태
- `idle-held`: utilization은 낮지만 프로세스가 GPU 메모리를 잡고 있는 상태
- `truly-idle`: 의미 있는 GPU 프로세스 메모리가 없는 상태

핵심은 `idle-held`입니다. 예를 들어 Jupyter notebook이 1% utilization으로 보이더라도 8 GB tensor를 계속 잡고 있으면 다른 사용자는 그 GPU를 쓰기 어렵습니다. 일반 dashboard에서는 이런 상태가 단순 idle처럼 보이기 쉽고, 이 도구는 그 차이를 드러냅니다.

보는 방법은 둘입니다. **라이브**는 `gua top`(1초 util 그래프 + GPU별 프로세스 표, 터미널에서 바로), **회고**는 `gua report`(SQLite에 기록한 뒤 구간을 위 세 상태 + GPU-hours 단위 idle 용량으로 분리). 여러 호스트를 쓰는 공용 랩이라면 선택적인 [GUA Board](#cloud-sync-gua-board-선택)가 같은 telemetry를 한 웹 화면으로 묶어줍니다.

## 주요 기능

- 단일 베어메탈 NVIDIA 호스트 감사
- `gua doctor`로 `/dev/nvidia*`, `nvidia-smi`, NVML, DB 경로 readiness 확인
- `gua daemon`, `gua status`, `gua stop` 기반 background collector
- `gua top` 로컬 라이브 뷰 — 1초 util 그래프 + GPU별 프로세스 표, 보드 불필요
- 기본 SQLite history DB: `~/.gua/gua.db`
- headline split, idle capacity, per-GPU 상태, top identities, time-of-day heatmap 리포트
- daemon run별 interval을 DB에 기록해 30초/10초 수집 run이 섞여도 GPU-hours 계산 유지
- GPU가 없어도 실행 가능한 deterministic `gua demo`
- [GUA Board](#cloud-sync-gua-board-선택)로의 선택적 cloud sync — 같은 1초 util 스트림을 여러 호스트에서 한 웹 화면으로
- Kubernetes, Slurm, Docker, remote node scan은 다루지 않음

## 설치

권장 설치 방법은 [uv](https://docs.astral.sh/uv/)를 통한 PyPI 설치입니다.

```sh
uv tool install gpu-usage-audit
```

업데이트와 제거:

```sh
uv tool install gpu-usage-audit@latest   # 방금 올라온 릴리스를 잡음(upgrade는 index 캐시로 놓칠 수 있음)
uv tool uninstall gpu-usage-audit
```

GitHub Releases에서 wheel을 직접 받을 수도 있습니다([최신 tag](https://github.com/AI-Ocean/gpu-usage-audit/releases)로 교체).

```sh
BASE="https://github.com/AI-Ocean/gpu-usage-audit/releases/download/v1.6.1"
WHEEL="gpu_usage_audit-1.6.1-py3-none-any.whl"

curl -fsSLO "$BASE/$WHEEL"
curl -fsSLO "$BASE/SHA256SUMS"
sha256sum -c SHA256SUMS --ignore-missing

uvx --from "./$WHEEL" gua doctor
```

## 빠른 시작

NVIDIA GPU 호스트에서:

```sh
gua doctor
gua daemon --interval 30s
gua status
gua report --since 1h
gua stop
```

`gua doctor`는 read-only입니다. `sudo`가 필요하지 않고, daemon을 실행할 사용자와 같은 사용자로 실행하는 것이 좋습니다.

daemon이나 리포트 없이 바로 보고 싶으면 `gua top`을 실행하세요(`q` 또는 Ctrl-C로 종료).

```sh
gua top            # 1초 util 그래프 + GPU별 프로세스 표
gua top --fake     # GPU 없는 머신에서도 체험
```

기본 상태 파일은 `~/.gua/` 아래에 저장됩니다.

| 경로 | 용도 |
| --- | --- |
| `~/.gua/gua.db` | SQLite history database |
| `~/.gua/gua.pid` | background daemon PID file |
| `~/.gua/gua.log` | daemon stdout/stderr log |

기본 DB는 append 가능한 local history DB입니다. 나중에 daemon을 다시 실행해도 같은 DB에 이어서 기록됩니다. 반대로 custom `--db PATH`를 지정하면 기존 파일이 있을 때 daemon이 거부하므로, 임시 수집 run이 실수로 섞이지 않습니다.

## 리포트 예시

```text
$ gua report --since 1h
gua — lab-a100 (bare, driver 560.35.05)  Window: 1:00:00

§1 Headline
  basis: one sample = one GPU card at one daemon tick
  rules: active >=10% util; idle-held <10% util with >100 MB process memory
  active       █   15.7%
  idle-held    ▒   45.1%
  truly-idle   ░   39.2%
  (51 samples)

§2 Idle capacity
  converted from card-ticks to GPU-hours using recorded daemon interval
  idle-held: ~0.31 GPU-hours, ~1.53 GPUs equivalently unavailable
  truly-idle: ~0.12 GPU-hours, ~1.00 GPUs equivalently free

§3 Per-GPU
§4 Top identities
§5 Time-of-day heatmap (UTC)
```

리포트는 daemon이 쓰는 중에도 실행할 수 있습니다. SQLite WAL mode가 concurrent read를 처리합니다. daemon을 멈춘 뒤에도 DB 파일이 남아 있으면 리포트를 읽을 수 있습니다.

## 명령어

| 명령 | 설명 |
| --- | --- |
| `gua doctor` | 로컬 NVIDIA/NVML readiness와 DB 경로 상태 확인 |
| `gua daemon` | 로컬 NVIDIA 호스트에서 background collection 시작 (`--cloud`면 GUA Board로 스트리밍) |
| `gua start` | `gua daemon` alias |
| `gua status` | managed background collector 실행 상태 확인 |
| `gua stop` | managed background collector 종료 |
| `gua top` | 로컬 라이브 GPU 뷰(1초 util 그래프 + 프로세스), 보드 불필요 |
| `gua report` | SQLite에서 retrospective report 출력 |
| `gua demo` | GPU 없이 fake telemetry 리포트 출력 |
| `gua enroll` | 이 호스트를 GUA Board workspace에 연결 (optional cloud sync) |
| `gua sync-once` | 한 snapshot을 수집해 latest 상태를 GUA Board로 push |
| `gua version` | 버전 출력 |

## 주요 옵션

```sh
gua daemon [--db PATH] [--interval D] [--pid-file PATH] [--log-file PATH]
gua daemon --cloud [--config PATH]        # GUA Board로도 스트리밍 (`gua enroll` 이후)
gua daemon --foreground [--db PATH] [--interval D]
gua top [--interval D] [--fake]
gua report [--db PATH] [--since D] [--interval D] [--width N]
gua demo [--db PATH] [--ticks N] [--interval D]
```

- daemon의 `--interval`은 수집 주기를 정합니다. 기본값은 `30s`입니다.
- report의 `--interval`은 선택적 override입니다. 새 DB row는 daemon run에 기록된 interval을 사용합니다. interval metadata가 없는 legacy row를 해석하거나 강제로 재계산할 때만 report `--interval D`를 사용하세요.
- `--since`는 `ms`, `s`, `m`, `h`, `d` 단위를 받으며 상한은 없습니다.
- `--foreground`는 systemd와 debugging 용도입니다.

## GPU 없이 데모 실행

```sh
gua demo
```

데모는 deterministic fake telemetry를 기록한 뒤 곧바로 리포트 형식을 출력합니다.

## systemd 예시

```ini
[Unit]
Description=gua daemon
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/gua daemon --foreground --db /var/lib/gua/gua.db --interval 30s
Restart=on-failure
User=gua

[Install]
WantedBy=multi-user.target
```

실행:

```sh
systemctl enable --now gpu-usage-audit
```

## Cloud Sync (GUA Board, 선택)

`gpu-usage-audit`은 기본적으로 완전히 로컬로 동작합니다. **GUA Board**는 여러 호스트의 같은 telemetry를 한 웹 화면으로 묶는 별도 서비스입니다 — 라이브 utilization 그래프를 예약 타임라인 옆에 두어, 공용 랩에서 어떤 GPU가 진짜 비었고, 예약됐고, *예약은 됐는데 놀고 있는지*를 한눈에 봅니다.

![GUA Board — 여러 호스트의 라이브 가용 현황](https://raw.githubusercontent.com/AI-Ocean/gpu-usage-audit/main/docs/img/board.png)

호스트 연결은 세 단계입니다.

```sh
# 1. GUA Board 웹에서 서버를 등록하고 one-time enrollment token을 복사합니다.
# 2. GPU 호스트에서:
gua enroll --server-url https://board.example.com --enrollment-token <TOKEN>
# 3a. 라이브: daemon을 cloud 모드로 — snapshot push + 1초 util을 WebSocket으로 스트리밍:
gua daemon --cloud
# 3b. 또는 단발: snapshot 하나 수집해 latest 상태만 push (타이머로 주기 실행):
gua sync-once
```

동작 방식과 하지 않는 것:

- `enroll`은 one-time token을 host-scoped write-only agent token으로 교환해 `~/.gua/cloud.json`(mode `0600`)에 저장합니다. 이 token은 이 호스트의 observation만 write할 수 있고, reservation/사용자/다른 host는 읽지 못합니다.
- `daemon --cloud`는 평소처럼 로컬 history를 계속 기록하면서, 추가로 1초 util 표본을 보드로 스트리밍하고(보드 그래프가 라이브로 흐름) 주기적 snapshot을 push합니다. 보드는 util을 메모리에만 버퍼하며 초단위 history를 저장하지 않습니다.
- `sync-once`는 한 snapshot을 수집해 **먼저 로컬 DB에 기록한 뒤** latest 상태만 push합니다. push 실패는 로컬 write를 막거나 되돌리지 않습니다.
- 항상 latest 상태만 전송합니다. 과거 tick은 로컬에 남고 서버로 replay되지 않습니다.
- process 정보는 PID, Linux user, process name(`/proc/<pid>/comm`), GPU memory로 제한되며 full command line은 절대 수집하지 않습니다.
- 에이전트는 바깥으로 push만 합니다. tunnel도, pull도, 원격 명령 실행도 없습니다 — 보드가 호스트 안으로 들어올 수 없습니다.

config/DB 경로는 `--config PATH` / `--db PATH`로 바꿀 수 있고, `gua sync-once --fake`로 GPU 없이 흐름을 확인할 수 있습니다.

## 분류 규칙

daemon은 매 tick마다 GPU별 utilization과 process별 GPU memory를 기록합니다. 리포트는 각 GPU card-tick을 다음 규칙으로 분류합니다.

```text
util >= 10                  -> active
util <  10 AND mem >  100   -> idle-held
util <  10 AND mem <= 100   -> truly-idle
```

100 MB threshold는 PyTorch/TensorFlow import 같은 runtime baseline을 흡수하기 위한 값입니다.

## 개발

```sh
git clone https://github.com/AI-Ocean/gpu-usage-audit
cd gpu-usage-audit
uv sync
uv run python -m pytest
uv run ruff check
uv run ruff format --check
uv run python -m mypy
uv run gua demo
```

CI는 ruff, format check, mypy, pytest, build, wheel smoke test를 실행합니다. `v*` tag push는 release asset을 만들고 Trusted Publishing으로 PyPI에 배포합니다.

## 범위 밖

이 도구는 단일 호스트 도구입니다 — 실행 중인 머신의 GPU를 라이브(`gua top`)와 회고(`gua report`)로 봅니다. cluster scheduler와는 통합하지 않습니다: Kubernetes cluster scan, Slurm join, quota, Docker/Podman runtime fallback, pod-name resolution 없음. 에이전트는 다른 호스트를 스캔하거나 거기로 접근하지 않습니다. 여러 호스트를 한 라이브 화면으로 묶는 일은 선택적인 [GUA Board](#cloud-sync-gua-board-선택)의 몫이고, 에이전트는 거기로 push만 합니다.

Go v0.1.0 구현은 tag `v0.1.0`과 [`go-archive`](https://github.com/AI-Ocean/gpu-usage-audit/tree/go-archive) branch에 남아 있습니다.

## 라이선스

Apache License 2.0. [LICENSE](LICENSE)를 참고하세요.
