package main

import (
	"context"
	"database/sql"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"os/signal"
	"os/user"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"syscall"
	"time"

	// modernc.org/sqlite 는 init() 안에서 sql 드라이버를 "sqlite" 이름으로
	// 등록한다. 이 패키지의 함수를 직접 부르지 않으므로 blank import.
	// 등록이 끝나면 database/sql 이 그 드라이버를 표준 인터페이스로 쓴다.
	_ "modernc.org/sqlite"
)

// version 은 빌드 타임에 -ldflags="-X main.version=..." 로 덮어쓰는 슬롯.
// 반드시 var (const 가 아니라) — linker 는 const 를 변경할 수 없다.
var version = "dev"

// ── NVML 이 한 틱에 주는 데이터의 모양 ─────────────────────────────

type GPUSample struct {
	UUID    string
	UtilPct int
}

type ProcSample struct {
	GPUUUID      string
	PID          int
	MemUsedMB    int
	LoginUIDUser *string // nil = unmapped (정상 케이스 포함)
}

type Snapshot struct {
	GPUs  []GPUSample
	Procs []ProcSample
}

// ── 분류 ──────────────────────────────────────────────────────────

type Sample struct {
	UtilPct   int
	ProcMemMB int
}

type Class string

const (
	Active    Class = "active"
	IdleHeld  Class = "idle-held"
	TrulyIdle Class = "truly-idle"
)

func Classify(s Sample) Class {
	if s.UtilPct >= 10 {
		return Active
	}
	if s.ProcMemMB > 100 {
		return IdleHeld
	}
	return TrulyIdle
}

// ── PID → 사용자명 ────────────────────────────────────────────────

const LoginUIDUnset uint32 = 4294967295

type UserLookupFunc func(pid int) (*string, error)

func TableUserLookup(table map[int]string) UserLookupFunc {
	return func(pid int) (*string, error) {
		if name, ok := table[pid]; ok {
			return &name, nil
		}
		return nil, nil
	}
}

func SystemUserLookup(pid int) (*string, error) {
	path := fmt.Sprintf("/proc/%d/loginuid", pid)
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	s := strings.TrimSpace(string(data))
	uid, err := strconv.ParseUint(s, 10, 32)
	if err != nil {
		return nil, fmt.Errorf("parse loginuid %q: %w", s, err)
	}
	if uint32(uid) == LoginUIDUnset {
		return nil, nil
	}
	u, err := user.LookupId(s)
	if err != nil {
		var unknown user.UnknownUserIdError
		if errors.As(err, &unknown) {
			return nil, nil
		}
		return nil, fmt.Errorf("lookup uid %s: %w", s, err)
	}
	return &u.Username, nil
}

func Resolve(snap Snapshot, lookup UserLookupFunc) Snapshot {
	resolved := make([]ProcSample, len(snap.Procs))
	for i, p := range snap.Procs {
		name, err := lookup(p.PID)
		if err != nil {
			fmt.Fprintf(os.Stderr, "resolve pid %d: %v\n", p.PID, err)
			name = nil
		}
		p.LoginUIDUser = name
		resolved[i] = p
	}
	snap.Procs = resolved
	return snap
}

// ── 영속화: 한 틱을 SQLite 에 적재 ───────────────────────────────

// schema 는 매 실행에 idempotent 하게 적용된다. v2 의 최소 스키마는
// 카드 단위 신호 테이블 하나, 프로세스 단위 신호 테이블 하나 — 딱 NVML 의
// 두 차원에 대응한다.
//
// gpu_sample 에 일부러 proc_mem_mb 를 *두지 않는다*. 카드별 메모리 합은
// 쿼리 시점에 proc_sample 의 JOIN + SUM 으로 계산할 거다. 즉 Aggregate/
// Summarize 에서 Go 로 했던 합산이 B 단계에서는 SQL 로 옮겨간다 —
// 그 변환을 직접 보는 게 학습 포인트.
const schema = `
CREATE TABLE IF NOT EXISTS host (
    hostname       TEXT NOT NULL,
    env_kind       TEXT NOT NULL,
    driver_version TEXT,
    first_seen     DATETIME NOT NULL,
    last_seen      DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS gpu_sample (
    ts        DATETIME NOT NULL,
    gpu_uuid  TEXT     NOT NULL,
    util_pct  INTEGER  NOT NULL
);

CREATE TABLE IF NOT EXISTS proc_sample (
    ts            DATETIME NOT NULL,
    gpu_uuid      TEXT     NOT NULL,
    pid           INTEGER  NOT NULL,
    mem_used_mb   INTEGER  NOT NULL,
    loginuid_user TEXT
);

-- (gpu_uuid, ts) 컬럼 순서: report 쿼리들이 *카드 단위* 로 시간 범위를
-- 자르는 패턴 (헤드라인/waste/유저별 모두) → uuid 가 leading column.
-- IF NOT EXISTS 로 매 실행 idempotent.
CREATE INDEX IF NOT EXISTS idx_gpu_sample_uuid_ts  ON gpu_sample(gpu_uuid, ts);
CREATE INDEX IF NOT EXISTS idx_proc_sample_uuid_ts ON proc_sample(gpu_uuid, ts);
`

// OpenDB 는 SQLite 파일을 열고 PRAGMA + 스키마를 적용한다.
// "sqlite" 드라이버 이름은 위쪽 blank import 가 등록해 둔 이름.
//
// PRAGMA 는 *스키마 적용 전* 에 박는다. WAL 모드는 DB 단위 영속 설정이라
// 한 번만 잡아도 파일에 기록되지만, busy_timeout 은 *연결 단위* 라 매
// OpenDB 마다 다시 설정해야 한다 — 둘을 같이 두는 게 안전.
//
// WAL: 데몬이 write 하는 동안 report 가 같은 DB 를 읽을 수 있게.
//      rollback journal 모드는 write 시 reader 를 막아 SQLITE_BUSY 유발.
// busy_timeout=5000: 그래도 *writer 끼리* 충돌 가능 (예: 향후 두 데몬).
//      즉시 실패 대신 5초까지 자동 재시도.
func OpenDB(ctx context.Context, path string) (*sql.DB, error) {
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, fmt.Errorf("open %s: %w", path, err)
	}
	// PRAGMA journal_mode 는 쿼리 형태라 Exec 대신 QueryRow 로 결과를
	// 읽어야 안전 — 드라이버에 따라 Exec 가 결과 row 를 버려서 모드 변경
	// 실패를 *조용히* 삼킬 수 있다.
	var mode string
	if err := db.QueryRowContext(ctx, "PRAGMA journal_mode=WAL").Scan(&mode); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("set journal_mode=WAL: %w", err)
	}
	if mode != "wal" {
		_ = db.Close()
		return nil, fmt.Errorf("expected journal_mode=wal, got %q", mode)
	}
	if _, err := db.ExecContext(ctx, "PRAGMA busy_timeout=5000"); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("set busy_timeout: %w", err)
	}
	if _, err := db.ExecContext(ctx, schema); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("apply schema: %w", err)
	}
	return db, nil
}

// HostMeta 는 데몬이 startup 에 한 번 결정해서 *수명 내내 들고 다니는*
// 호스트 컨텍스트. hostname/env_kind/driver_version 은 데몬 lifetime 동안
// 변하지 않는다는 가정. firstSeen 은 host 행의 immutable 필드 (재시작 후에도
// 첫 INSERT 시각 보존), lastSeen 은 매 틱 갱신.
type HostMeta struct {
	Hostname      string
	EnvKind       string
	DriverVersion string
	FirstSeen     time.Time
}

// DetectEnvKind 는 /proc/1/cgroup 을 보고 bare/docker/k8s 를 분류한다.
// PID 1 은 부팅 직후 커널이 띄우는 init — bare 면 systemd 의 systemd-managed
// 경로, 컨테이너 안이면 docker/containerd/kubepods 경로가 등장.
//
// 매칭 우선순위: k8s → docker → bare → unknown.
// k8s 를 먼저 보는 이유: k8s 파드는 내부적으로 docker/containerd 위에
// 도는 경우가 흔해서 docker 시그니처가 false positive 가 될 수 있다.
// unknown 은 silent 폴백 — 알 수 없는 환경을 "bare 인 척" 하면 위험.
func DetectEnvKind(procRoot string) string {
	path := filepath.Join(procRoot, "1", "cgroup")
	data, err := os.ReadFile(path)
	if err != nil {
		return "unknown"
	}
	s := string(data)
	switch {
	case strings.Contains(s, "kubepods"):
		return "k8s"
	case strings.Contains(s, "docker"), strings.Contains(s, "containerd"):
		return "docker"
	}
	// cgroup 라인 형식: "<hierarchy>:<controllers>:<path>" (v1)
	// 또는 "0::<path>" (v2). 마지막 필드가 systemd 관리 경로면 bare.
	for _, line := range strings.Split(s, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		parts := strings.SplitN(line, ":", 3)
		if len(parts) != 3 {
			continue
		}
		p := parts[2]
		if p == "/" || p == "/init.scope" ||
			strings.HasPrefix(p, "/system.slice") ||
			strings.HasPrefix(p, "/user.slice") {
			return "bare"
		}
	}
	return "unknown"
}

// UpsertHost 는 단일 row 의 호스트 메타를 유지한다. UPSERT 패턴은
// "UPDATE 시도 → 영향받은 행 0 이면 INSERT". 우리 schema 에 hostname
// UNIQUE 제약이 없어서 ON CONFLICT 를 못 쓰는 단순 시나리오 — 직접
// 패턴 구현.
//
// 갱신 정책:
//   env_kind, driver_version, last_seen → 매 틱 갱신
//   hostname, first_seen                → 한 번 박히면 immutable
// first_seen 이 immutable 이라 데몬 재시작해도 첫 적재 시각 보존됨.
func UpsertHost(ctx context.Context, tx *sql.Tx, h HostMeta, lastSeen time.Time) error {
	res, err := tx.ExecContext(ctx,
		`UPDATE host SET env_kind=?, driver_version=?, last_seen=? WHERE hostname=?`,
		h.EnvKind, h.DriverVersion, lastSeen, h.Hostname)
	if err != nil {
		return fmt.Errorf("update host: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("rows affected: %w", err)
	}
	if n > 0 {
		return nil
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO host(hostname, env_kind, driver_version, first_seen, last_seen) VALUES(?,?,?,?,?)`,
		h.Hostname, h.EnvKind, h.DriverVersion, h.FirstSeen, lastSeen,
	); err != nil {
		return fmt.Errorf("insert host: %w", err)
	}
	return nil
}

// WriteSnapshot 은 한 틱의 Snapshot + 호스트 메타를 *단일 트랜잭션* 으로
// 적재한다. host 까지 같이 묶는 이유: 한 ts 의 모든 사실이 *원자적으로*
// 들어가야 일관성이 유지됨. host 만 따로 커밋되고 gpu_sample 실패 같은
// 부분 상태가 만들어지지 않게.
//
// defer tx.Rollback() 은 어떤 경로로 빠져나가더라도 트랜잭션 정리.
// Commit() 후의 Rollback 은 sql.ErrTxDone no-op 이라 안전.
//
// p.LoginUIDUser 가 nil 일 때 driver 가 자동으로 SQL NULL — *string
// 타입이 그 매핑을 트리거 (string 이면 빈 문자열로 가버림).
func WriteSnapshot(ctx context.Context, db *sql.DB, ts time.Time, host HostMeta, snap Snapshot) (err error) {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	if err := UpsertHost(ctx, tx, host, ts); err != nil {
		return err
	}
	for _, g := range snap.GPUs {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO gpu_sample(ts, gpu_uuid, util_pct) VALUES(?,?,?)`,
			ts, g.UUID, g.UtilPct,
		); err != nil {
			return fmt.Errorf("insert gpu_sample: %w", err)
		}
	}
	for _, p := range snap.Procs {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO proc_sample(ts, gpu_uuid, pid, mem_used_mb, loginuid_user) VALUES(?,?,?,?,?)`,
			ts, p.GPUUUID, p.PID, p.MemUsedMB, p.LoginUIDUser,
		); err != nil {
			return fmt.Errorf("insert proc_sample: %w", err)
		}
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit: %w", err)
	}
	return nil
}

// ── 데이터 소스 추상: Tier ────────────────────────────────────────

// Tier 는 "한 틱의 GPU 텔레메트리를 어디서 받아오는가" 의 추상.
// 운영용 NVMLTier (미구현) 와 학습/테스트용 FakeTier 가 같은 자리에 꽂힌다.
//
// Probe: 데몬 시작 시 한 번 호출. 드라이버 버전 등 *변하지 않는 메타* 를
//        받는 용도. 진짜 NVML 에선 nvml.Init + SystemGetDriverVersion.
// Collect: 매 틱 호출. ts 는 데몬이 캡처한 한 틱의 시각.
type Tier interface {
	Probe(ctx context.Context) (driverVersion string, err error)
	Collect(ctx context.Context, ts time.Time) (Snapshot, error)
}

// FakeTier 는 내부 tick 카운터에 따라 결정적으로 변동하는 가짜 데이터를
// 만든다. 매 호출마다 다른 모양이 나와야 데몬 루프와 fraction 집계의
// 의미가 살아난다.
//
// GPU-0 의 5틱 주기 (학습 → idle-held → cleanup 의 워크로드를 *압축*):
//   0..1  학습 활성 — util 80, 메모리 70GB
//   2..3  학습 끝, 메모리는 못 놓음 — util 2, 메모리 70GB (= idle-held)
//   4     정리 후 truly-idle — util 0, 메모리 0
// 진짜 데몬에선 분/시간 단위로 일어날 일이지만, 데모용으로 5틱에 압축.
//
// GPU-1: 항상 Jupyter 가 8GB 잡고 있음 → 매 틱 idle-held
// GPU-2: 항상 truly-idle
type FakeTier struct {
	tick int // Collect 호출마다 1씩 증가. 외부에서 건드리지 않음.
}

// Probe 는 가짜 드라이버 버전을 반환. 실제 NVML 머신이면
// nvml.SystemGetDriverVersion() 의 진짜 값이 들어옴.
func (*FakeTier) Probe(_ context.Context) (string, error) {
	return "560.35.05-fake", nil
}

func (f *FakeTier) Collect(_ context.Context, _ time.Time) (Snapshot, error) {
	phase := f.tick % 5
	f.tick++

	var gpu0Util, gpu0Mem int
	switch {
	case phase < 2:
		gpu0Util, gpu0Mem = 80, 70000
	case phase < 4:
		gpu0Util, gpu0Mem = 2, 70000
	default:
		gpu0Util, gpu0Mem = 0, 0
	}

	procs := []ProcSample{
		{GPUUUID: "GPU-1", PID: 5678, MemUsedMB: 8000},
		{GPUUUID: "GPU-1", PID: 9999, MemUsedMB: 200},
	}
	// 메모리 0 이면 그 프로세스는 카드 위에 없는 것 — NVML 도 그렇게 본다.
	if gpu0Mem > 0 {
		procs = append([]ProcSample{
			{GPUUUID: "GPU-0", PID: 1234, MemUsedMB: gpu0Mem},
		}, procs...)
	}

	return Snapshot{
		GPUs: []GPUSample{
			{UUID: "GPU-0", UtilPct: gpu0Util},
			{UUID: "GPU-1", UtilPct: 2},
			{UUID: "GPU-2", UtilPct: 0},
		},
		Procs: procs,
	}, nil
}

// ── 카드 단위 결과 + 프로세스 디테일 ──────────────────────────────

type CardSummary struct {
	UUID      string
	UtilPct   int
	ProcMemMB int
	Class     Class
	Procs     []ProcSample
}

func Summarize(snap Snapshot) []CardSummary {
	procsByGPU := make(map[string][]ProcSample, len(snap.GPUs))
	for _, p := range snap.Procs {
		procsByGPU[p.GPUUUID] = append(procsByGPU[p.GPUUUID], p)
	}
	for uuid := range procsByGPU {
		slices.SortFunc(procsByGPU[uuid], func(a, b ProcSample) int {
			return b.MemUsedMB - a.MemUsedMB
		})
	}
	out := make([]CardSummary, 0, len(snap.GPUs))
	for _, g := range snap.GPUs {
		procs := procsByGPU[g.UUID]
		mem := 0
		for _, p := range procs {
			mem += p.MemUsedMB
		}
		out = append(out, CardSummary{
			UUID:      g.UUID,
			UtilPct:   g.UtilPct,
			ProcMemMB: mem,
			Class:     Classify(Sample{UtilPct: g.UtilPct, ProcMemMB: mem}),
			Procs:     procs,
		})
	}
	return out
}

// ── 데몬 루프 ────────────────────────────────────────────────────

// runDaemon 은 ctx 캔슬까지 interval 간격으로 한 틱씩 반복한다.
//
// anti-drift 스케줄: 다음 타겟을 next + interval 로 잡되, 틱이 너무 오래
// 걸려서 이미 지났으면 *catch-up 하지 않고* now 로 점프. NVML/DB 가 잠시
// 느려진 뒤 back-to-back 으로 펌프질하지 않게 — 그러면 단기적으로 idle
// 비율 계산이 망가지고 DB 가 부풀어 오른다.
//
// 틱 에러는 로그만 찍고 다음 틱 계속. 일시적 hiccup (NVML 일시 응답 없음
// 등) 으로 데몬이 죽으면 운영에서 그 자체가 사고. 영구 에러면 매 틱마다
// 로그가 쌓여 운영자가 알아챔.
//
// context.Canceled 는 *정상 종료* (시그널 받음) 로 nil 반환. 그 외 ctx
// 에러 (DeadlineExceeded 등) 는 그대로 반환해서 호출자가 알게.
func runDaemon(ctx context.Context, tier Tier, lookup UserLookupFunc, db *sql.DB, host HostMeta, interval time.Duration) error {
	log.Printf("daemon started (host=%s, env=%s, driver=%s, interval=%s)",
		host.Hostname, host.EnvKind, host.DriverVersion, interval)
	next := time.Now()
	tickN := 0
	for {
		if err := ctx.Err(); err != nil {
			if errors.Is(err, context.Canceled) {
				log.Printf("shutdown signal received, daemon stopping cleanly (%d ticks)", tickN)
				return nil
			}
			return err
		}

		if err := tick(ctx, tier, lookup, db, host, tickN); err != nil {
			log.Printf("tick %d failed; continuing: %v", tickN, err)
		}
		tickN++

		next = next.Add(interval)
		now := time.Now()
		if now.After(next) {
			log.Printf("tick overran schedule by %s; jumping without catch-up", now.Sub(next))
			next = now
		}

		if err := sleepUntil(ctx, next); err != nil {
			if errors.Is(err, context.Canceled) {
				log.Printf("shutdown signal received during sleep (%d ticks)", tickN)
				return nil
			}
			return err
		}
	}
}

// tick 은 한 틱의 모든 일을 한 군데로 모은다 — Collect → Resolve →
// WriteSnapshot (host 도 같이 upsert) + 사람용 한 줄 요약. runDaemon 에서
// 분리해두면 (1) 에러 처리 경계가 깔끔하고 (2) 미래에 테스트가 한 틱만
// 호출해 검증 가능.
func tick(ctx context.Context, tier Tier, lookup UserLookupFunc, db *sql.DB, host HostMeta, n int) error {
	ts := time.Now().UTC()

	snap, err := tier.Collect(ctx, ts)
	if err != nil {
		return fmt.Errorf("collect: %w", err)
	}
	snap = Resolve(snap, lookup)
	if err := WriteSnapshot(ctx, db, ts, host, snap); err != nil {
		return fmt.Errorf("write: %w", err)
	}

	classes := make([]string, 0, len(snap.GPUs))
	for _, c := range Summarize(snap) {
		classes = append(classes, fmt.Sprintf("%s=%-10s", c.UUID, c.Class))
	}
	fmt.Printf("Tick %d  ts=%s  %s\n", n, ts.Format("15:04:05.000"), strings.Join(classes, "  "))
	return nil
}

// sleepUntil 은 t 까지 슬립하되, ctx 캔슬에 *즉시* 응답한다.
// time.After 와 ctx.Done 두 채널을 select 로 경합 — 어느 쪽이 먼저 와도
// 정상. 이미 지난 t 면 즉시 반환 (catch-up 안 함).
//
// time.NewTimer + defer Stop 은 다 못 기다리고 빠져나갈 때 timer goroutine
// 누수를 막는 정석 패턴.
func sleepUntil(ctx context.Context, t time.Time) error {
	d := time.Until(t)
	if d <= 0 {
		return nil
	}
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-timer.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// ── 리포트: host 헤더 + §1 Headline ─────────────────────────────

// HostRow 는 host 테이블의 한 행을 report 가 읽기 좋게 받는 모양.
// driver_version 이 nullable (TEXT) 이라 빈 문자열 표현 가능 — *string 으로
// 받아도 되지만 한 줄 헤더 표현용이라 빈 문자열로 충분.
type HostRow struct {
	Hostname      string
	EnvKind       string
	DriverVersion string
}

// LoadHost 는 단일 host 행을 읽는다. row 없으면 (sql.ErrNoRows) 빈
// HostRow 와 nil — 헤더는 "데몬 적재 전 DB" 로 가볍게 표시.
func LoadHost(ctx context.Context, db *sql.DB) (HostRow, error) {
	var h HostRow
	var driver sql.NullString
	err := db.QueryRowContext(ctx,
		`SELECT hostname, env_kind, driver_version FROM host LIMIT 1`,
	).Scan(&h.Hostname, &h.EnvKind, &driver)
	if errors.Is(err, sql.ErrNoRows) {
		return HostRow{}, nil
	}
	if err != nil {
		return HostRow{}, fmt.Errorf("load host: %w", err)
	}
	h.DriverVersion = driver.String
	return h, nil
}



// headlineQuery 는 시간 윈도우 [cutoff, ∞) 안의 모든 gpu_sample 을 (gpu, ts)
// 단위로 그루핑하면서, 같은 (gpu, ts) 의 proc_sample 메모리를 LEFT JOIN +
// SUM 으로 합쳐 한 줄로 만든다. Go 의 Aggregate/Summarize 함수가 *런타임*
// 메모리에서 했던 일을 SQL 이 *DB 시점에* 한 셈.
//
// 그 합쳐진 행들에 대해 세 카테고리의 시간 비중 (AVG(CASE)) 을 한 번에 낸다.
// CASE 가 1.0/0.0 을 반환하므로 AVG 가 곧 fraction.
//
// LEFT JOIN 이 핵심: 프로세스 없는 카드도 (proc_mem_mb=0 으로) 살아남아야
// truly-idle 로 집계됨. INNER JOIN 이었다면 통째로 사라짐.
const headlineQuery = `
WITH s AS (
    SELECT gs.gpu_uuid, gs.ts, gs.util_pct,
           COALESCE(SUM(ps.mem_used_mb), 0) AS proc_mem_mb
    FROM gpu_sample gs
    LEFT JOIN proc_sample ps
        ON ps.gpu_uuid = gs.gpu_uuid AND ps.ts = gs.ts
    WHERE gs.ts >= ?
    GROUP BY gs.gpu_uuid, gs.ts
)
SELECT
    AVG(CASE WHEN util_pct >= 10                          THEN 1.0 ELSE 0.0 END) AS active,
    AVG(CASE WHEN util_pct <  10 AND proc_mem_mb >  100   THEN 1.0 ELSE 0.0 END) AS idle_held,
    AVG(CASE WHEN util_pct <  10 AND proc_mem_mb <= 100   THEN 1.0 ELSE 0.0 END) AS truly_idle,
    COUNT(*)                                                                     AS samples
FROM s
`

// Headline 은 §1 의 결과. 0 샘플 윈도우는 Samples==0 으로 표현 —
// 분수 값을 0 으로만 보면 "0% active" 인지 "데이터 없음" 인지 구별이 안 됨.
type Headline struct {
	Active    float64
	IdleHeld  float64
	TrulyIdle float64
	Samples   int
}

// LoadHeadline 은 cutoff 이후의 모든 샘플로 §1 fraction 을 낸다.
// 모든 카드를 한꺼번에 집계 — GPU 별 분해는 §3 의 일.
func LoadHeadline(ctx context.Context, db *sql.DB, cutoff time.Time) (Headline, error) {
	var active, idleHeld, trulyIdle sql.NullFloat64
	var samples int
	err := db.QueryRowContext(ctx, headlineQuery, cutoff).Scan(&active, &idleHeld, &trulyIdle, &samples)
	if err != nil {
		return Headline{}, fmt.Errorf("load headline: %w", err)
	}
	// sql.NullFloat64 인 이유: 0 행이면 AVG 가 NULL. .Float64 는 그때 자동으로 0.
	return Headline{
		Active:    active.Float64,
		IdleHeld:  idleHeld.Float64,
		TrulyIdle: trulyIdle.Float64,
		Samples:   samples,
	}, nil
}

// 카테고리별로 다른 글자를 써서 색깔 없이도 시각적 구분이 되게 한다.
// (기존 프로젝트는 같은 █ 에 ANSI 색을 입혔지만, 우리는 isatty/색 토글
// 복잡도 없이 글자 자체로 구분.)
const (
	glyphActive    = "█" // U+2588 가장 진한 블록
	glyphIdleHeld  = "▒" // U+2592 중간 음영
	glyphTrulyIdle = "░" // U+2591 가장 옅은 블록
)

// renderHeadline 은 호스트 헤더 + §1 결과를 한 줄짜리 3-bar 와 비율로 출력.
// width 는 막대 총 너비. 비례 분할 시 active/idle-held 를 반올림으로 잡고
// 나머지를 truly-idle 에 줘서 합이 항상 width — 마지막 칸이 비어 보이지
// 않게.
func renderHeadline(w io.Writer, host HostRow, h Headline, since time.Duration, width int) {
	if host.Hostname == "" {
		fmt.Fprintf(w, "gpu-usage-audit  (no host row — daemon hasn't run yet?)  Window: %s\n\n", since)
	} else {
		ctx := host.EnvKind
		if host.DriverVersion != "" {
			ctx = fmt.Sprintf("%s, driver %s", host.EnvKind, host.DriverVersion)
		}
		fmt.Fprintf(w, "gpu-usage-audit — %s (%s)  Window: %s\n\n", host.Hostname, ctx, since)
	}
	fmt.Fprintln(w, "§1 Headline")
	if h.Samples == 0 {
		fmt.Fprintln(w, "  (no samples in window)")
		return
	}
	wA := int(h.Active*float64(width) + 0.5)
	wB := int(h.IdleHeld*float64(width) + 0.5)
	wC := width - wA - wB
	if wC < 0 {
		wC = 0
	}
	bar := strings.Repeat(glyphActive, wA) +
		strings.Repeat(glyphIdleHeld, wB) +
		strings.Repeat(glyphTrulyIdle, wC)
	fmt.Fprintf(w, "  %s\n", bar)
	fmt.Fprintf(w, "  active       %s  %5.1f%%\n", glyphActive, h.Active*100)
	fmt.Fprintf(w, "  idle-held    %s  %5.1f%%\n", glyphIdleHeld, h.IdleHeld*100)
	fmt.Fprintf(w, "  truly-idle   %s  %5.1f%%\n", glyphTrulyIdle, h.TrulyIdle*100)
	fmt.Fprintf(w, "  (%d samples)\n", h.Samples)
}

// ── 리포트: §2 Waste ─────────────────────────────────────────────

// wasteQuery: idle 틱 수 × interval(초) / 3600 = idle GPU-시간.
// equiv_unused = idle 비율 × 카드 수. "8장 중 3.2장이 통째로 놀았다" 식.
// 카드 수는 *gpu_sample 에서 distinct* 로 추론 — v2 는 별도 gpu 인벤토리
// 테이블이 없어서 (v1 의 단순화). interval 은 Go 에서 인자로 받는다 —
// 데몬과 report 가 *같은* interval 을 약속해야 의미가 맞음.
const wasteQuery = `
SELECT
    SUM(CASE WHEN util_pct < 10 THEN 1 ELSE 0 END) * ? / 3600.0          AS idle_gpu_hours,
    CASE WHEN COUNT(*) = 0 THEN 0.0
         ELSE SUM(CASE WHEN util_pct < 10 THEN 1.0 ELSE 0.0 END) / COUNT(*)
              * (SELECT COUNT(DISTINCT gpu_uuid) FROM gpu_sample)
    END                                                                  AS equiv_unused,
    COUNT(*)                                                             AS samples
FROM gpu_sample
WHERE ts >= ?
`

type Waste struct {
	IdleGPUHours float64
	EquivUnused  float64
	Samples      int
}

func LoadWaste(ctx context.Context, db *sql.DB, cutoff time.Time, interval time.Duration) (Waste, error) {
	var w Waste
	var idleHours, equiv sql.NullFloat64
	err := db.QueryRowContext(ctx, wasteQuery, interval.Seconds(), cutoff).
		Scan(&idleHours, &equiv, &w.Samples)
	if err != nil {
		return Waste{}, fmt.Errorf("load waste: %w", err)
	}
	w.IdleGPUHours = idleHours.Float64
	w.EquivUnused = equiv.Float64
	return w, nil
}

func renderWaste(w io.Writer, waste Waste) {
	fmt.Fprintln(w)
	fmt.Fprintln(w, "§2 Waste")
	if waste.Samples == 0 {
		fmt.Fprintln(w, "  (no samples in window)")
		return
	}
	fmt.Fprintf(w, "  ~%.2f GPU-hours idle, ~%.2f GPUs equivalently unused\n",
		waste.IdleGPUHours, waste.EquivUnused)
}

// ── 리포트: §3 Per-GPU ────────────────────────────────────────────

// perGPUQuery: §1 과 같은 분류룰 (util≥10, mem>100) 을 GPU 별로 분해.
// 핵심: GROUP BY gs.gpu_uuid 가 카드 단위 분해. proc_mem 은 sub-query 로
// 미리 (gpu, ts) 별 합산해서 LEFT JOIN — v1 의 perGPUQuery 와 같은 패턴.
const perGPUQuery = `
SELECT
    gs.gpu_uuid,
    AVG(CASE WHEN gs.util_pct >= 10                                      THEN 1.0 ELSE 0.0 END) AS active,
    AVG(CASE WHEN gs.util_pct <  10 AND COALESCE(ps.proc_mem, 0) >  100  THEN 1.0 ELSE 0.0 END) AS idle_held,
    AVG(CASE WHEN gs.util_pct <  10 AND COALESCE(ps.proc_mem, 0) <= 100  THEN 1.0 ELSE 0.0 END) AS truly_idle,
    COUNT(*)                                                                                    AS samples
FROM gpu_sample gs
LEFT JOIN (
    SELECT gpu_uuid, ts, SUM(mem_used_mb) AS proc_mem
    FROM proc_sample
    GROUP BY gpu_uuid, ts
) ps ON ps.gpu_uuid = gs.gpu_uuid AND ps.ts = gs.ts
WHERE gs.ts >= ?
GROUP BY gs.gpu_uuid
ORDER BY gs.gpu_uuid
`

type PerGPU struct {
	UUID      string
	Active    float64
	IdleHeld  float64
	TrulyIdle float64
	Samples   int
}

func LoadPerGPU(ctx context.Context, db *sql.DB, cutoff time.Time) ([]PerGPU, error) {
	rows, err := db.QueryContext(ctx, perGPUQuery, cutoff)
	if err != nil {
		return nil, fmt.Errorf("load per-gpu: %w", err)
	}
	defer func() { _ = rows.Close() }()
	var out []PerGPU
	for rows.Next() {
		var p PerGPU
		var active, idleHeld, trulyIdle sql.NullFloat64
		if err := rows.Scan(&p.UUID, &active, &idleHeld, &trulyIdle, &p.Samples); err != nil {
			return nil, fmt.Errorf("scan per-gpu: %w", err)
		}
		p.Active = active.Float64
		p.IdleHeld = idleHeld.Float64
		p.TrulyIdle = trulyIdle.Float64
		out = append(out, p)
	}
	return out, rows.Err()
}

func renderPerGPU(w io.Writer, rows []PerGPU) {
	fmt.Fprintln(w)
	fmt.Fprintln(w, "§3 Per-GPU")
	if len(rows) == 0 {
		fmt.Fprintln(w, "  (no GPU cards in window)")
		return
	}
	for _, r := range rows {
		fmt.Fprintf(w, "  %-8s  active %5.1f%%  idle-held %5.1f%%  truly-idle %5.1f%%  (%d samples)\n",
			r.UUID, r.Active*100, r.IdleHeld*100, r.TrulyIdle*100, r.Samples)
	}
}

// ── 리포트: §4 Top identities ────────────────────────────────────

// topIdentitiesQuery: 누가 GPU-시간을 가장 많이 소비했나 + 그 중 idle-held
// 비율. v1 은 proc_sample.util_pct 를 직접 썼지만 v2 는 그 컬럼이 없어서
// gpu_sample 의 카드 util 을 JOIN 으로 가져온다 — 의미: "이 사용자가 카드
// 잡고 있던 시간 중, *카드가 idle 인데* 이 프로세스가 메모리 100MB 이상
// 잡고 있던 비율". v1 의 의미와 미묘하게 다른 해석.
//
// COALESCE 로 NULL loginuid_user 를 'unknown' 으로 묶음 — §4 에 한 줄로
// 보이는 게 합당.
const topIdentitiesQuery = `
SELECT
    COALESCE(ps.loginuid_user, 'unknown')                                                      AS identity,
    COUNT(*) * ? / 3600.0                                                                      AS gpu_hours,
    AVG(CASE WHEN gs.util_pct < 10 AND ps.mem_used_mb > 100 THEN 1.0 ELSE 0.0 END)             AS idle_held
FROM proc_sample ps
JOIN gpu_sample gs ON gs.gpu_uuid = ps.gpu_uuid AND gs.ts = ps.ts
WHERE ps.ts >= ?
GROUP BY identity
ORDER BY gpu_hours DESC
LIMIT 10
`

type TopIdentity struct {
	Identity string
	GPUHours float64
	IdleHeld float64
}

func LoadTopIdentities(ctx context.Context, db *sql.DB, cutoff time.Time, interval time.Duration) ([]TopIdentity, error) {
	rows, err := db.QueryContext(ctx, topIdentitiesQuery, interval.Seconds(), cutoff)
	if err != nil {
		return nil, fmt.Errorf("load top identities: %w", err)
	}
	defer func() { _ = rows.Close() }()
	var out []TopIdentity
	for rows.Next() {
		var t TopIdentity
		var idleHeld sql.NullFloat64
		if err := rows.Scan(&t.Identity, &t.GPUHours, &idleHeld); err != nil {
			return nil, fmt.Errorf("scan top identity: %w", err)
		}
		t.IdleHeld = idleHeld.Float64
		out = append(out, t)
	}
	return out, rows.Err()
}

func renderTopIdentities(w io.Writer, rows []TopIdentity) {
	fmt.Fprintln(w)
	fmt.Fprintln(w, "§4 Top identities")
	if len(rows) == 0 {
		fmt.Fprintln(w, "  (no processes in window)")
		return
	}
	fmt.Fprintf(w, "  %-20s %10s  %10s\n", "identity", "gpu-hours", "idle-held")
	for _, r := range rows {
		fmt.Fprintf(w, "  %-20s %10.2f  %9.1f%%\n", r.Identity, r.GPUHours, r.IdleHeld*100)
	}
}

// ── 리포트: §5 Heatmap ────────────────────────────────────────────

// heatmapQuery: ts 의 *요일×시간* 으로 그루핑해서 각 셀의 active 비율.
// substr(ts, 1, 19): 우리 ts 가 Go time.Time.String() 형식 (`2026-05-11
// 06:00:19.922...`) 이라 앞 19자만 잘라 `2026-05-11 06:00:19` 로 만들면
// SQLite strftime 이 ISO datetime 으로 해석 가능. 나노초/타임존을 떼는 효과.
// 0=일요일 .. 6=토요일 (strftime %w 규칙).
const heatmapQuery = `
SELECT
    CAST(strftime('%w', substr(ts, 1, 19)) AS INTEGER) AS dow,
    CAST(strftime('%H', substr(ts, 1, 19)) AS INTEGER) AS hour,
    AVG(CASE WHEN util_pct >= 10 THEN 1.0 ELSE 0.0 END) AS active,
    COUNT(*)                                            AS samples
FROM gpu_sample
WHERE ts >= ?
GROUP BY dow, hour
ORDER BY dow, hour
`

type HeatmapCell struct {
	Dow     int
	Hour    int
	Active  float64
	Samples int
}

func LoadHeatmap(ctx context.Context, db *sql.DB, cutoff time.Time) ([]HeatmapCell, error) {
	rows, err := db.QueryContext(ctx, heatmapQuery, cutoff)
	if err != nil {
		return nil, fmt.Errorf("load heatmap: %w", err)
	}
	defer func() { _ = rows.Close() }()
	var out []HeatmapCell
	for rows.Next() {
		var c HeatmapCell
		if err := rows.Scan(&c.Dow, &c.Hour, &c.Active, &c.Samples); err != nil {
			return nil, fmt.Errorf("scan heatmap cell: %w", err)
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

// active 비율 [0,1] 을 10단계 ASCII 농도 문자에 매핑.
// 빈 셀(데이터 없음) 과 0% 활성 셀을 구별하기 위해 빈 셀은 별도로 ' .' 처리.
const heatmapDensity = " .:-=+*#%@"

var dowLabels = [7]string{"Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"}

func renderHeatmap(w io.Writer, cells []HeatmapCell) {
	fmt.Fprintln(w)
	fmt.Fprintln(w, "§5 Time-of-day heatmap (UTC)")
	if len(cells) == 0 {
		fmt.Fprintln(w, "  (no samples in window)")
		return
	}

	// 7×24 그리드로 채우면서, 데이터 *있음/없음* 을 별도 마스크에 기록.
	var grid [7][24]float64
	var seen [7][24]bool
	for _, c := range cells {
		if c.Dow < 0 || c.Dow > 6 || c.Hour < 0 || c.Hour > 23 {
			continue
		}
		grid[c.Dow][c.Hour] = c.Active
		seen[c.Dow][c.Hour] = true
	}

	// 시간 헤더 줄 — 24개라 2자리는 좁아서 hour%10 한 자리로.
	fmt.Fprint(w, "       ")
	for h := range 24 {
		fmt.Fprintf(w, "%2d", h%10)
	}
	fmt.Fprintln(w)

	for dow := range 7 {
		fmt.Fprintf(w, "  %s  ", dowLabels[dow])
		for h := range 24 {
			if !seen[dow][h] {
				// 비셀 = 공백 두 칸. heatmapDensity 의 ' '/'.' 와 시각적으로 분리.
				// (v1 은 ANSI 색으로 구분했지만 v2 는 색 없이 글자만으로.)
				fmt.Fprint(w, "  ")
				continue
			}
			v := grid[dow][h]
			if v < 0 {
				v = 0
			}
			if v > 1 {
				v = 1
			}
			idx := int(v * float64(len(heatmapDensity)-1))
			fmt.Fprintf(w, " %c", heatmapDensity[idx])
		}
		fmt.Fprintln(w)
	}
}

// ── 메인: 서브커맨드 디스패처 ─────────────────────────────────────

func userOrUnknown(name *string) string {
	if name == nil {
		return "unknown"
	}
	return *name
}

const usage = `Usage: gpu-usage-audit <command> [flags]

Commands:
  daemon    Sample GPU/process telemetry into SQLite at a fixed interval
  report    Print §1–§5 retrospective report from an accumulated database
  help      Show this message
  version   Print version

Flags:
  --help, -h     Show this message
  --version      Print version

Use "gpu-usage-audit <command> -h" for command-specific flags.
`

// main 은 첫 인자를 보고 daemon/report 로 분기하는 *디스패처*.
// 기존 단일 모드에서 두 서브커맨드로 분리 — daemon 은 쓰기, report 는 읽기.
// 같은 바이너리이지만 lifetime 과 ctx 정책이 완전히 다른 두 모드.
//
// help/version 은 *명령 형태* 와 *플래그 형태* 모두 받는다 — `kubectl
// version` 처럼 동작하길 기대하는 사용자와 `--version` 만 기대하는
// 사용자가 모두 자연스럽게 닿게.
func main() {
	if len(os.Args) < 2 {
		fmt.Fprint(os.Stderr, usage)
		os.Exit(2)
	}
	switch os.Args[1] {
	case "daemon":
		os.Exit(runDaemonCmd(os.Args[2:]))
	case "report":
		os.Exit(runReportCmd(os.Args[2:]))
	case "version", "--version", "-version":
		fmt.Println(version)
	case "help", "--help", "-help", "-h":
		fmt.Print(usage)
	default:
		fmt.Fprintf(os.Stderr, "gpu-usage-audit: unknown command %q\n\n", os.Args[1])
		fmt.Fprint(os.Stderr, usage)
		os.Exit(2)
	}
}

// runDaemonCmd 는 G 에서 만든 데몬 루프를 서브커맨드로 감싼 것.
// 종료 코드 0 (정상), 1 (런타임 실패), 2 (사용법 오류).
func runDaemonCmd(args []string) int {
	fs := flag.NewFlagSet("gpu-usage-audit daemon", flag.ExitOnError)
	dbPath := fs.String("db", "", "Path to SQLite database file (required)")
	interval := fs.Duration("interval", 30*time.Second, "Tick interval (e.g. 30s, 1m, 200ms)")
	_ = fs.Parse(args)
	if *dbPath == "" {
		fmt.Fprintln(os.Stderr, "gpu-usage-audit daemon: -db is required")
		return 2
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	db, err := OpenDB(ctx, *dbPath)
	if err != nil {
		log.Print(err)
		return 1
	}
	defer func() { _ = db.Close() }()

	tier := &FakeTier{}
	lookup := TableUserLookup(map[int]string{
		1234: "alice",
		5678: "bob",
	})

	// startup 메타 수집 (Probe 한 번 + 환경/hostname 한 번).
	// 데몬 lifetime 동안 변하지 않는 값들이므로 한 번만 결정해 들고 다닌다.
	driverVersion, err := tier.Probe(ctx)
	if err != nil {
		log.Printf("Probe failed: %v (continuing with empty driver_version)", err)
	}
	hostname, err := os.Hostname()
	if err != nil {
		hostname = "unknown"
	}
	host := HostMeta{
		Hostname:      hostname,
		EnvKind:       DetectEnvKind("/proc"),
		DriverVersion: driverVersion,
		FirstSeen:     time.Now().UTC(),
	}

	if err := runDaemon(ctx, tier, lookup, db, host, *interval); err != nil {
		log.Print(err)
		return 1
	}

	var total int
	if err := db.QueryRowContext(context.Background(),
		"SELECT COUNT(*) FROM gpu_sample").Scan(&total); err == nil {
		fmt.Printf("\n%s: %d total gpu_sample rows\n", *dbPath, total)
	}
	return 0
}

// runReportCmd 는 누적 DB 를 *읽기 전용* 으로 열어 §1 Headline 을 낸다.
// 데몬과 *동시에* 돌 수 있도록 OpenDB 가 journal_mode=WAL 을 잡고
// busy_timeout 으로 짧은 락 충돌을 흡수한다.
func runReportCmd(args []string) int {
	fs := flag.NewFlagSet("gpu-usage-audit report", flag.ExitOnError)
	dbPath := fs.String("db", "", "Path to SQLite database file (required)")
	since := fs.Duration("since", time.Hour, "Report window (e.g. 1h, 24h, 5m)")
	interval := fs.Duration("interval", 30*time.Second, "Daemon tick interval — needed for §2 Waste / §4 time conversion")
	width := fs.Int("width", 60, "Width of the headline bar")
	_ = fs.Parse(args)
	if *dbPath == "" {
		fmt.Fprintln(os.Stderr, "gpu-usage-audit report: -db is required")
		return 2
	}

	ctx := context.Background()
	db, err := OpenDB(ctx, *dbPath)
	if err != nil {
		log.Print(err)
		return 1
	}
	defer func() { _ = db.Close() }()

	// cutoff 는 *지금* 기준 since 만큼 과거. UTC 로 통일 — 적재 시점과
	// 같은 형식이어야 SQLite 의 문자열 비교가 chronological 한다.
	cutoff := time.Now().UTC().Add(-*since)

	host, err := LoadHost(ctx, db)
	if err != nil {
		log.Print(err)
		return 1
	}
	headline, err := LoadHeadline(ctx, db, cutoff)
	if err != nil {
		log.Print(err)
		return 1
	}
	waste, err := LoadWaste(ctx, db, cutoff, *interval)
	if err != nil {
		log.Print(err)
		return 1
	}
	perGPU, err := LoadPerGPU(ctx, db, cutoff)
	if err != nil {
		log.Print(err)
		return 1
	}
	topIdent, err := LoadTopIdentities(ctx, db, cutoff, *interval)
	if err != nil {
		log.Print(err)
		return 1
	}
	heatmap, err := LoadHeatmap(ctx, db, cutoff)
	if err != nil {
		log.Print(err)
		return 1
	}

	renderHeadline(os.Stdout, host, headline, *since, *width)
	renderWaste(os.Stdout, waste)
	renderPerGPU(os.Stdout, perGPU)
	renderTopIdentities(os.Stdout, topIdent)
	renderHeatmap(os.Stdout, heatmap)
	return 0
}
