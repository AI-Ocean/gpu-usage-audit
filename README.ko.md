# gpu-usage-audit

로컬 NVIDIA 호스트에서 GPU 사용 기록을 수집하고, **util은 낮지만 메모리를 잡고 있어 못 쓰는 GPU**를 `idle-held`로 따로 보여주는 감사 도구입니다.

[![PyPI](https://img.shields.io/pypi/v/gpu-usage-audit.svg)](https://pypi.org/project/gpu-usage-audit/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://pypi.org/project/gpu-usage-audit/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![GitHub Release](https://img.shields.io/github/v/release/AI-Ocean/gpu-usage-audit)](https://github.com/AI-Ocean/gpu-usage-audit/releases)

[English](README.md) · [한국어](README.ko.md) · [Releases](https://github.com/AI-Ocean/gpu-usage-audit/releases) · [Issues](https://github.com/AI-Ocean/gpu-usage-audit/issues)

---

## 소개

gpu-usage-audit는 로컬 NVIDIA/NVML telemetry를 SQLite에 저장하고, 이후 리포트에서 GPU card-tick을 다음 세 상태로 나눠 보여줍니다.

- `active`: 실제 연산이 일어나는 상태
- `idle-held`: utilization은 낮지만 프로세스가 GPU 메모리를 잡고 있는 상태
- `truly-idle`: 의미 있는 GPU 프로세스 메모리가 없는 상태

핵심은 `idle-held`입니다. 예를 들어 Jupyter notebook이 1% utilization으로 보이더라도 8 GB tensor를 계속 잡고 있으면 다른 사용자는 그 GPU를 쓰기 어렵습니다. 일반 dashboard에서는 이런 상태가 단순 idle처럼 보이기 쉽고, 이 도구는 그 차이를 리포트로 드러냅니다.

## 주요 기능

- 단일 베어메탈 NVIDIA 호스트 감사
- `gua doctor`로 `/dev/nvidia*`, `nvidia-smi`, NVML, DB 경로 readiness 확인
- `gua daemon`, `gua status`, `gua stop` 기반 background collector
- 기본 SQLite history DB: `~/.gua/gua.db`
- headline split, idle capacity, per-GPU 상태, top identities, time-of-day heatmap 리포트
- daemon run별 interval을 DB에 기록해 30초/10초 수집 run이 섞여도 GPU-hours 계산 유지
- GPU가 없어도 실행 가능한 deterministic `gua demo`
- 1.0 범위에서는 Kubernetes, Slurm, Docker, remote node scan을 다루지 않음

## 설치

권장 설치 방법은 [uv](https://docs.astral.sh/uv/)를 통한 PyPI 설치입니다.

```sh
uv tool install gpu-usage-audit
```

업데이트와 제거:

```sh
uv tool upgrade gpu-usage-audit
uv tool uninstall gpu-usage-audit
```

GitHub Releases에서 wheel을 직접 받을 수도 있습니다.

```sh
BASE="https://github.com/AI-Ocean/gpu-usage-audit/releases/download/v1.0.3"
WHEEL="gpu_usage_audit-1.0.3-py3-none-any.whl"

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
| `gua daemon` | 로컬 NVIDIA 호스트에서 background collection 시작 |
| `gua start` | `gua daemon` alias |
| `gua status` | managed background collector 실행 상태 확인 |
| `gua stop` | managed background collector 종료 |
| `gua report` | SQLite에서 retrospective report 출력 |
| `gua demo` | GPU 없이 fake telemetry 리포트 출력 |
| `gua enroll` | 이 호스트를 GUA Board workspace에 연결 (optional cloud sync) |
| `gua sync-once` | 한 snapshot을 수집해 latest 상태를 GUA Board로 push |
| `gua version` | 버전 출력 |

## 주요 옵션

```sh
gua daemon [--db PATH] [--interval D] [--pid-file PATH] [--log-file PATH]
gua daemon --foreground [--db PATH] [--interval D]
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

`gpu-usage-audit`은 기본적으로 완전히 로컬로 동작합니다. 여러 서버의 최신 GPU 가용 상태를 한 화면에서 보는 [GUA Board](https://github.com/AI-Ocean/gpu-usage-audit)를 함께 쓴다면, 호스트를 선택적으로 연결할 수 있습니다.

```sh
# 1. GUA Board 웹에서 서버를 등록하고 one-time enrollment token을 복사합니다.
# 2. GPU 호스트에서:
gua enroll --server-url https://board.example.com --enrollment-token <TOKEN>
# 3. 현재 snapshot을 push (타이머로 주기 실행하거나 `gua daemon`과 함께):
gua sync-once
```

동작 방식과 하지 않는 것:

- `enroll`은 one-time token을 host-scoped write-only agent token으로 교환해 `~/.gua/cloud.json`(mode `0600`)에 저장합니다. 이 token은 이 호스트의 observation만 write할 수 있고, reservation/사용자/다른 host는 읽지 못합니다.
- `sync-once`는 한 snapshot을 수집해 **먼저 로컬 DB에 기록한 뒤** latest 상태만 push합니다. push 실패는 로컬 write를 막거나 되돌리지 않습니다.
- 항상 latest snapshot만 전송합니다. 과거 tick은 로컬에 남고 서버로 replay되지 않습니다.
- process 정보는 PID, Linux user, process name(`/proc/<pid>/comm`), GPU memory로 제한되며 full command line은 절대 수집하지 않습니다.
- cloud sync는 신규 런타임 의존성을 추가하지 않습니다(표준 라이브러리 사용).

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

이 도구는 단일 호스트 retrospective audit 도구입니다. Live dashboard, multi-host aggregation, quota, Kubernetes cluster scan, Slurm join, Docker/Podman runtime fallback, pod-name resolution은 bare-metal 1.0 범위 밖입니다.

Go v0.1.0 구현은 tag `v0.1.0`과 [`go-archive`](https://github.com/AI-Ocean/gpu-usage-audit/tree/go-archive) branch에 남아 있습니다.

## 라이선스

Apache License 2.0. [LICENSE](LICENSE)를 참고하세요.
