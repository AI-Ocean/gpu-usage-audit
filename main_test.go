package main

import (
	"context"
	"database/sql"
	"math"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// ── Classify ─────────────────────────────────────────────────────
//
// 분류 규칙 (main.go 의 Classify):
//   util >= 10              → active
//   util < 10 && mem > 100  → idle-held
//   util < 10 && mem <= 100 → truly-idle
//
// 경계값 케이스 (util=10, mem=100) 를 콕 짚는 게 핵심. 임계가 ">=" 인지
// ">" 인지가 다음 사람이 코드 안 봐도 테스트만 봐도 알 수 있게.
func TestClassify(t *testing.T) {
	cases := []struct {
		name string
		util int
		mem  int
		want Class
	}{
		{"util 정확히 임계", 10, 0, Active},
		{"util 임계 직전", 9, 0, TrulyIdle},
		{"util 임계 위 + 메모리 큼", 80, 70000, Active},
		{"util 낮음 + 메모리 임계 위", 2, 101, IdleHeld},
		{"util 낮음 + 메모리 정확히 임계", 2, 100, TrulyIdle},
		{"util 0 + 메모리 0", 0, 0, TrulyIdle},
		{"util 음수 — 방어적", -1, 0, TrulyIdle},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := Classify(Sample{UtilPct: tc.util, ProcMemMB: tc.mem})
			if got != tc.want {
				t.Errorf("Classify(util=%d, mem=%d) = %q, want %q", tc.util, tc.mem, got, tc.want)
			}
		})
	}
}

// ── DetectEnvKind ────────────────────────────────────────────────
//
// 가짜 procRoot 를 t.TempDir() 에 깔고 1/cgroup 파일의 내용을 바꿔가며
// 분류를 검증한다. 실제 /proc 을 건드리지 않으므로 컨테이너/CI 어디서나
// 동일하게 동작.
//
// 우선순위 케이스: k8s 시그니처와 docker 시그니처가 *동시에* 있을 때
// k8s 로 분류돼야 함. (k8s 파드가 내부적으로 containerd 위에서 돌기
// 때문에 docker 가 false positive 가 될 수 있다는 게 동기.)
func TestDetectEnvKind(t *testing.T) {
	cases := []struct {
		name    string
		content string // nil 이면 파일 자체 안 만듦
		exists  bool
		want    string
	}{
		{
			name:    "k8s — kubepods 경로",
			content: "12:devices:/kubepods/besteffort/pod-abc/container-xyz\n",
			exists:  true,
			want:    "k8s",
		},
		{
			name:    "k8s 우선순위 — kubepods + docker 둘 다",
			content: "12:devices:/kubepods/...\n11:cpu:/docker/abc\n",
			exists:  true,
			want:    "k8s",
		},
		{
			name:    "docker — docker 경로",
			content: "12:devices:/docker/abcdef\n",
			exists:  true,
			want:    "docker",
		},
		{
			name:    "docker — containerd 경로",
			content: "12:devices:/containerd/xyz\n",
			exists:  true,
			want:    "docker",
		},
		{
			name:    "bare — system.slice",
			content: "0::/system.slice/gpu-audit.service\n",
			exists:  true,
			want:    "bare",
		},
		{
			name:    "bare — init.scope",
			content: "0::/init.scope\n",
			exists:  true,
			want:    "bare",
		},
		{
			name:    "bare — 루트 경로",
			content: "0::/\n",
			exists:  true,
			want:    "bare",
		},
		{
			name:    "bare — user.slice",
			content: "0::/user.slice/user-1000.slice\n",
			exists:  true,
			want:    "bare",
		},
		{
			name:    "unknown — 모르는 경로",
			content: "0::/some/weird/path\n",
			exists:  true,
			want:    "unknown",
		},
		{
			name:   "unknown — 파일 자체 없음",
			exists: false,
			want:   "unknown",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := t.TempDir()
			if tc.exists {
				dir := filepath.Join(root, "1")
				if err := os.MkdirAll(dir, 0o755); err != nil {
					t.Fatalf("mkdir: %v", err)
				}
				if err := os.WriteFile(filepath.Join(dir, "cgroup"), []byte(tc.content), 0o644); err != nil {
					t.Fatalf("write cgroup: %v", err)
				}
			}
			got := DetectEnvKind(root)
			if got != tc.want {
				t.Errorf("DetectEnvKind(...) = %q, want %q\n  content=%q", got, tc.want, tc.content)
			}
		})
	}
}

// ── Summarize ────────────────────────────────────────────────────
//
// Summarize 는 한 틱의 Snapshot 을 카드 단위로 접는다. 검증할 것:
//   1. 카드별 메모리 = 그 카드의 proc 메모리 합.
//   2. Class 가 Classify(util, 합산메모리) 와 일치.
//   3. Procs 가 MemUsedMB *내림차순* 정렬.
//   4. *알 수 없는 GPU UUID* 에 매달린 proc 은 결과에서 빠진다
//      (snap.GPUs 에 없는 uuid 의 proc 은 보고되지 않음).
//   5. proc 0개인 카드는 mem=0, truly-idle 로 분류.
func TestSummarize(t *testing.T) {
	owner := "alice"
	snap := Snapshot{
		GPUs: []GPUSample{
			{UUID: "GPU-0", UtilPct: 80}, // active, 학습 중
			{UUID: "GPU-1", UtilPct: 2},  // idle-held 예정 (메모리 잡혀있음)
			{UUID: "GPU-2", UtilPct: 0},  // truly-idle (proc 없음)
		},
		Procs: []ProcSample{
			{GPUUUID: "GPU-0", PID: 100, MemUsedMB: 30000, LoginUIDUser: &owner},
			{GPUUUID: "GPU-0", PID: 101, MemUsedMB: 40000, LoginUIDUser: &owner},
			{GPUUUID: "GPU-1", PID: 200, MemUsedMB: 70000, LoginUIDUser: &owner},
			// 알 수 없는 UUID — 드랍돼야 한다.
			{GPUUUID: "GPU-99", PID: 999, MemUsedMB: 1234, LoginUIDUser: nil},
		},
	}

	got := Summarize(snap)
	if len(got) != 3 {
		t.Fatalf("len(Summarize) = %d, want 3 (= len(snap.GPUs))", len(got))
	}

	// (1)(2)(3): GPU-0 — 두 proc 합 70000, util 80 → active, 정렬 40000>30000
	g0 := got[0]
	if g0.UUID != "GPU-0" || g0.ProcMemMB != 70000 || g0.Class != Active {
		t.Errorf("GPU-0: uuid=%q mem=%d class=%q, want GPU-0/70000/active",
			g0.UUID, g0.ProcMemMB, g0.Class)
	}
	if len(g0.Procs) != 2 || g0.Procs[0].MemUsedMB != 40000 || g0.Procs[1].MemUsedMB != 30000 {
		t.Errorf("GPU-0 Procs 정렬 깨짐: %+v", g0.Procs)
	}

	// (2): GPU-1 — proc 메모리 70000, util 2 → idle-held
	g1 := got[1]
	if g1.UUID != "GPU-1" || g1.ProcMemMB != 70000 || g1.Class != IdleHeld {
		t.Errorf("GPU-1: uuid=%q mem=%d class=%q, want GPU-1/70000/idle-held",
			g1.UUID, g1.ProcMemMB, g1.Class)
	}

	// (5): GPU-2 — proc 없음 → mem=0, truly-idle
	g2 := got[2]
	if g2.UUID != "GPU-2" || g2.ProcMemMB != 0 || g2.Class != TrulyIdle || len(g2.Procs) != 0 {
		t.Errorf("GPU-2: uuid=%q mem=%d class=%q procs=%d, want GPU-2/0/truly-idle/0",
			g2.UUID, g2.ProcMemMB, g2.Class, len(g2.Procs))
	}

	// (4): GPU-99 의 proc 은 어디에도 안 들어가야.
	for _, c := range got {
		for _, p := range c.Procs {
			if p.PID == 999 {
				t.Errorf("알 수 없는 GPU 의 proc 이 카드 %s 에 들어갔다: %+v", c.UUID, p)
			}
		}
	}
}

// ── DB 함수 테스트용 fixture ─────────────────────────────────────
//
// 의도: report 측 5개 함수 (LoadHost, LoadHeadline, LoadWaste,
// LoadPerGPU, LoadTopIdentities) 가 *같은* fixture 위에서 돌게 해서
// 분류룰 · 시간환산 · 분해축이 모두 한 시나리오로 검증되게.
//
// 시나리오 (40초, 10초 간격, 2 카드, 2 사용자):
//   ts0:  GPU-0 util=80 alice 70GB ,  GPU-1 util=2 bob 70GB
//   ts10: GPU-0 util=80 alice 70GB ,  GPU-1 util=2 bob 70GB
//   ts20: GPU-0 util=2  (proc 없음),  GPU-1 util=2 bob 70GB
//   ts30: GPU-0 util=2  (proc 없음),  GPU-1 util=2 bob 70GB
//
// → gpu_sample 8 행, proc_sample 6 행.
// → 분류: active 2 (GPU-0 ts0,ts10), idle-held 4 (GPU-1 전체),
//         truly-idle 2 (GPU-0 ts20,ts30, proc 없음).

const fixtureInterval = 10 * time.Second

// openTestDB 는 t.TempDir() 안에 실제 파일 DB 를 만든다.
// OpenDB 와 동일 경로 (WAL + 인덱스 포함) — 운영과 같은 코드 경로 검증.
func openTestDB(t *testing.T) *sql.DB {
	t.Helper()
	path := filepath.Join(t.TempDir(), "test.db")
	db, err := OpenDB(context.Background(), path)
	if err != nil {
		t.Fatalf("OpenDB: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return db
}

// writeFixture 는 위 시나리오를 적재하고 (cutoff, interval) 을 돌려준다.
// cutoff 는 base 시각 그대로 — 모든 fixture 행이 윈도우 안에 들어옴.
func writeFixture(t *testing.T, db *sql.DB) (base time.Time, interval time.Duration) {
	t.Helper()
	ctx := context.Background()
	host := HostMeta{
		Hostname:      "testhost",
		EnvKind:       "bare",
		DriverVersion: "999.99-test",
		FirstSeen:     time.Date(2026, 5, 11, 0, 0, 0, 0, time.UTC),
	}
	alice := "alice"
	bob := "bob"
	base = host.FirstSeen
	interval = fixtureInterval

	// 각 ts 의 Snapshot 을 단일 트랜잭션으로 적재 (WriteSnapshot 의 동선).
	tick := func(off time.Duration, snap Snapshot) {
		if err := WriteSnapshot(ctx, db, base.Add(off), host, snap); err != nil {
			t.Fatalf("WriteSnapshot @%s: %v", off, err)
		}
	}

	tick(0*time.Second, Snapshot{
		GPUs: []GPUSample{{UUID: "GPU-0", UtilPct: 80}, {UUID: "GPU-1", UtilPct: 2}},
		Procs: []ProcSample{
			{GPUUUID: "GPU-0", PID: 100, MemUsedMB: 70000, LoginUIDUser: &alice},
			{GPUUUID: "GPU-1", PID: 200, MemUsedMB: 70000, LoginUIDUser: &bob},
		},
	})
	tick(10*time.Second, Snapshot{
		GPUs: []GPUSample{{UUID: "GPU-0", UtilPct: 80}, {UUID: "GPU-1", UtilPct: 2}},
		Procs: []ProcSample{
			{GPUUUID: "GPU-0", PID: 100, MemUsedMB: 70000, LoginUIDUser: &alice},
			{GPUUUID: "GPU-1", PID: 200, MemUsedMB: 70000, LoginUIDUser: &bob},
		},
	})
	tick(20*time.Second, Snapshot{
		GPUs: []GPUSample{{UUID: "GPU-0", UtilPct: 2}, {UUID: "GPU-1", UtilPct: 2}},
		Procs: []ProcSample{
			{GPUUUID: "GPU-1", PID: 200, MemUsedMB: 70000, LoginUIDUser: &bob},
		},
	})
	tick(30*time.Second, Snapshot{
		GPUs: []GPUSample{{UUID: "GPU-0", UtilPct: 2}, {UUID: "GPU-1", UtilPct: 2}},
		Procs: []ProcSample{
			{GPUUUID: "GPU-1", PID: 200, MemUsedMB: 70000, LoginUIDUser: &bob},
		},
	})
	return base, interval
}

// approxEq: float fraction 비교용. SQL AVG 와 우리 손계산의 부동소수점
// 차를 흡수. 1e-9 면 충분.
func approxEq(a, b float64) bool {
	return math.Abs(a-b) < 1e-9
}

// ── LoadHost ─────────────────────────────────────────────────────
//
// 빈 DB → sql.ErrNoRows 를 swallow 하고 (빈 HostRow, nil) 반환해야 함.
// (renderHeadline 헤더가 "host row 없음" 분기를 갖고 있어서 이 동작이
// 헤더 표시의 단순성을 떠받친다.)
func TestLoadHost_empty(t *testing.T) {
	db := openTestDB(t)
	h, err := LoadHost(context.Background(), db)
	if err != nil {
		t.Fatalf("LoadHost on empty DB: %v", err)
	}
	if h != (HostRow{}) {
		t.Errorf("LoadHost(empty) = %+v, want zero value", h)
	}
}

func TestLoadHost_populated(t *testing.T) {
	db := openTestDB(t)
	writeFixture(t, db)
	h, err := LoadHost(context.Background(), db)
	if err != nil {
		t.Fatalf("LoadHost: %v", err)
	}
	want := HostRow{Hostname: "testhost", EnvKind: "bare", DriverVersion: "999.99-test"}
	if h != want {
		t.Errorf("LoadHost = %+v, want %+v", h, want)
	}
}

// ── LoadHeadline ─────────────────────────────────────────────────
//
// 8 sample fixture → active 2/8, idle-held 4/8, truly-idle 2/8.
// 빈 DB 와 미래-cutoff 의 두 가지 "0 행" 경로가 똑같이 Samples=0 으로
// 떨어져야 (NullFloat64 가 .Float64=0 으로 흡수).
func TestLoadHeadline(t *testing.T) {
	db := openTestDB(t)
	base, _ := writeFixture(t, db)

	h, err := LoadHeadline(context.Background(), db, base)
	if err != nil {
		t.Fatalf("LoadHeadline: %v", err)
	}
	if h.Samples != 8 {
		t.Errorf("Samples = %d, want 8", h.Samples)
	}
	if !approxEq(h.Active, 2.0/8) {
		t.Errorf("Active = %v, want 0.25", h.Active)
	}
	if !approxEq(h.IdleHeld, 4.0/8) {
		t.Errorf("IdleHeld = %v, want 0.50", h.IdleHeld)
	}
	if !approxEq(h.TrulyIdle, 2.0/8) {
		t.Errorf("TrulyIdle = %v, want 0.25", h.TrulyIdle)
	}
}

func TestLoadHeadline_emptyDB(t *testing.T) {
	db := openTestDB(t)
	h, err := LoadHeadline(context.Background(), db, time.Now())
	if err != nil {
		t.Fatalf("LoadHeadline on empty DB: %v", err)
	}
	if h.Samples != 0 || h.Active != 0 || h.IdleHeld != 0 || h.TrulyIdle != 0 {
		t.Errorf("LoadHeadline(empty) = %+v, want zero Headline", h)
	}
}

func TestLoadHeadline_cutoffPastAll(t *testing.T) {
	db := openTestDB(t)
	base, _ := writeFixture(t, db)
	// 모든 행보다 *미래* 의 cutoff → 0 행.
	h, err := LoadHeadline(context.Background(), db, base.Add(time.Hour))
	if err != nil {
		t.Fatalf("LoadHeadline: %v", err)
	}
	if h.Samples != 0 {
		t.Errorf("Samples = %d, want 0 (cutoff past all)", h.Samples)
	}
}

// ── LoadWaste ────────────────────────────────────────────────────
//
// idle 틱 (util<10) 수 = 6. interval=10s.
//   idle_gpu_hours = 6 * 10 / 3600 ≈ 0.016667
//   equiv_unused   = 6/8 * 2 (distinct gpu)  = 1.5
func TestLoadWaste(t *testing.T) {
	db := openTestDB(t)
	base, interval := writeFixture(t, db)
	w, err := LoadWaste(context.Background(), db, base, interval)
	if err != nil {
		t.Fatalf("LoadWaste: %v", err)
	}
	if w.Samples != 8 {
		t.Errorf("Samples = %d, want 8", w.Samples)
	}
	wantIdleH := 6.0 * 10.0 / 3600.0
	if !approxEq(w.IdleGPUHours, wantIdleH) {
		t.Errorf("IdleGPUHours = %v, want %v", w.IdleGPUHours, wantIdleH)
	}
	if !approxEq(w.EquivUnused, 1.5) {
		t.Errorf("EquivUnused = %v, want 1.5", w.EquivUnused)
	}
}

// ── LoadPerGPU ───────────────────────────────────────────────────
//
// GPU-0: active 2/4, idle-held 0, truly-idle 2/4.
// GPU-1: active 0,   idle-held 4/4, truly-idle 0.
// ORDER BY gpu_uuid 라 GPU-0 가 먼저.
func TestLoadPerGPU(t *testing.T) {
	db := openTestDB(t)
	base, _ := writeFixture(t, db)
	rows, err := LoadPerGPU(context.Background(), db, base)
	if err != nil {
		t.Fatalf("LoadPerGPU: %v", err)
	}
	if len(rows) != 2 {
		t.Fatalf("len(rows) = %d, want 2", len(rows))
	}
	g0, g1 := rows[0], rows[1]
	if g0.UUID != "GPU-0" || !approxEq(g0.Active, 0.5) || !approxEq(g0.IdleHeld, 0) || !approxEq(g0.TrulyIdle, 0.5) || g0.Samples != 4 {
		t.Errorf("GPU-0 = %+v, want active=0.5 idle-held=0 truly-idle=0.5 samples=4", g0)
	}
	if g1.UUID != "GPU-1" || !approxEq(g1.Active, 0) || !approxEq(g1.IdleHeld, 1.0) || !approxEq(g1.TrulyIdle, 0) || g1.Samples != 4 {
		t.Errorf("GPU-1 = %+v, want active=0 idle-held=1.0 truly-idle=0 samples=4", g1)
	}
}

// ── LoadTopIdentities ────────────────────────────────────────────
//
// bob:   4 procs, 4*10/3600 ≈ 0.01111 GPU-h, 4/4 = 100% idle-held
//   (모두 GPU-1 util=2, mem=70000>100 → idle-held 조건 정확히 충족)
// alice: 2 procs, 2*10/3600 ≈ 0.00556 GPU-h, 0% idle-held
//   (모두 GPU-0 util=80 → util<10 분기 안 탐)
// ORDER BY gpu_hours DESC → bob 먼저.
func TestLoadTopIdentities(t *testing.T) {
	db := openTestDB(t)
	base, interval := writeFixture(t, db)
	rows, err := LoadTopIdentities(context.Background(), db, base, interval)
	if err != nil {
		t.Fatalf("LoadTopIdentities: %v", err)
	}
	if len(rows) != 2 {
		t.Fatalf("len(rows) = %d, want 2", len(rows))
	}
	b, a := rows[0], rows[1]
	if b.Identity != "bob" || !approxEq(b.GPUHours, 4.0*10.0/3600.0) || !approxEq(b.IdleHeld, 1.0) {
		t.Errorf("bob = %+v, want gpu-hours≈0.01111 idle-held=1.0", b)
	}
	if a.Identity != "alice" || !approxEq(a.GPUHours, 2.0*10.0/3600.0) || !approxEq(a.IdleHeld, 0) {
		t.Errorf("alice = %+v, want gpu-hours≈0.00556 idle-held=0", a)
	}
}

// ── LoadHeatmap ──────────────────────────────────────────────────
//
// 시간 분해를 의미 있게 검증하려면 *다른 (dow, hour) 셀* 에 데이터를
// 적재해야 한다. 위 writeFixture 는 40초라 한 셀에만 모이는 문제.
//
// 별도 시나리오:
//   2026-05-11 00:00:xx (월요일 UTC 자정대)  — 2 tick × 2 GPU = 4 sample
//     매 tick: GPU-0 util=80(active), GPU-1 util=2(idle) → active 2/4
//   2026-05-12 01:00:00 (화요일 새벽 1시대) — 1 tick × 2 GPU = 2 sample
//     active 1/2
//
// SQLite strftime('%w') 는 일=0..토=6 으로 Go time.Weekday() 와 동일
// 규약. 그래서 expected dow 는 time.Weekday() 로 계산해 두면 어떤
// 달력 변화에도 견디는 테스트가 된다 (날짜 자체를 바꾸지 않는 한).
func TestLoadHeatmap(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	host := HostMeta{
		Hostname:  "heatmap-host",
		EnvKind:   "bare",
		FirstSeen: time.Date(2026, 5, 11, 0, 0, 0, 0, time.UTC),
	}
	monMidnight := host.FirstSeen
	tueOneAM := time.Date(2026, 5, 12, 1, 0, 0, 0, time.UTC)

	// 셀 1: 월요일 자정대 — 2 tick, 1초 차이지만 같은 (dow=1, hour=0).
	for i := 0; i < 2; i++ {
		ts := monMidnight.Add(time.Duration(i) * time.Second)
		snap := Snapshot{GPUs: []GPUSample{
			{UUID: "GPU-0", UtilPct: 80},
			{UUID: "GPU-1", UtilPct: 2},
		}}
		if err := WriteSnapshot(ctx, db, ts, host, snap); err != nil {
			t.Fatalf("WriteSnapshot mon @%d: %v", i, err)
		}
	}

	// 셀 2: 화요일 1시대 — 1 tick.
	if err := WriteSnapshot(ctx, db, tueOneAM, host, Snapshot{GPUs: []GPUSample{
		{UUID: "GPU-0", UtilPct: 80},
		{UUID: "GPU-1", UtilPct: 2},
	}}); err != nil {
		t.Fatalf("WriteSnapshot tue: %v", err)
	}

	cells, err := LoadHeatmap(ctx, db, monMidnight)
	if err != nil {
		t.Fatalf("LoadHeatmap: %v", err)
	}
	if len(cells) != 2 {
		t.Fatalf("len(cells) = %d, want 2 (mon-0h, tue-1h)", len(cells))
	}

	wantMon := HeatmapCell{Dow: int(monMidnight.Weekday()), Hour: 0, Active: 0.5, Samples: 4}
	wantTue := HeatmapCell{Dow: int(tueOneAM.Weekday()), Hour: 1, Active: 0.5, Samples: 2}

	// ORDER BY dow, hour → mon 먼저 (Weekday 값이 작음).
	if cells[0].Dow != wantMon.Dow || cells[0].Hour != wantMon.Hour ||
		!approxEq(cells[0].Active, wantMon.Active) || cells[0].Samples != wantMon.Samples {
		t.Errorf("cells[0] = %+v, want %+v", cells[0], wantMon)
	}
	if cells[1].Dow != wantTue.Dow || cells[1].Hour != wantTue.Hour ||
		!approxEq(cells[1].Active, wantTue.Active) || cells[1].Samples != wantTue.Samples {
		t.Errorf("cells[1] = %+v, want %+v", cells[1], wantTue)
	}
}

func TestLoadHeatmap_emptyDB(t *testing.T) {
	db := openTestDB(t)
	cells, err := LoadHeatmap(context.Background(), db, time.Now())
	if err != nil {
		t.Fatalf("LoadHeatmap on empty DB: %v", err)
	}
	if len(cells) != 0 {
		t.Errorf("len(cells) = %d, want 0", len(cells))
	}
}

// ── FakeTier 5틱 phase 사이클 ─────────────────────────────────────
//
// FakeTier 가 데몬 데모와 §1 fraction 의 의미를 살리는 *결정적* 시퀀스를
// 만든다는 게 학습의 핵심. 5틱 주기 + 사이클 반복을 한 번에 검증.
//
// GPU-0 (압축 워크로드):
//   tick 0..1 → util=80, mem=70000  (active)
//   tick 2..3 → util=2,  mem=70000  (idle-held)
//   tick 4    → util=0,  mem=0       (truly-idle, proc 없음)
//   tick 5+   → 다시 0번 패턴 반복
//
// GPU-1, GPU-2 는 매 틱 같음 — 1 틱만 검증해도 충분.
func TestFakeTier_phaseCycle(t *testing.T) {
	f := &FakeTier{}
	ctx := context.Background()

	type want struct {
		util int
		mem  int // GPU-0 의 proc 메모리 합. 0 이면 proc 없음.
	}
	expect := []want{
		{80, 70000}, // tick 0
		{80, 70000}, // tick 1
		{2, 70000},  // tick 2
		{2, 70000},  // tick 3
		{0, 0},      // tick 4 — proc 없음
		{80, 70000}, // tick 5 — 사이클 반복
		{80, 70000}, // tick 6
	}

	for i, w := range expect {
		snap, err := f.Collect(ctx, time.Time{})
		if err != nil {
			t.Fatalf("tick %d Collect: %v", i, err)
		}

		// GPU-0 util
		var gpu0Util int = -1
		for _, g := range snap.GPUs {
			if g.UUID == "GPU-0" {
				gpu0Util = g.UtilPct
				break
			}
		}
		if gpu0Util != w.util {
			t.Errorf("tick %d: GPU-0 util = %d, want %d", i, gpu0Util, w.util)
		}

		// GPU-0 메모리 = 해당 카드의 proc 메모리 합
		gpu0Mem := 0
		for _, p := range snap.Procs {
			if p.GPUUUID == "GPU-0" {
				gpu0Mem += p.MemUsedMB
			}
		}
		if gpu0Mem != w.mem {
			t.Errorf("tick %d: GPU-0 mem = %d, want %d", i, gpu0Mem, w.mem)
		}

		// GPU-1, GPU-2 불변량
		var sawGPU1, sawGPU2 bool
		for _, g := range snap.GPUs {
			switch g.UUID {
			case "GPU-1":
				sawGPU1 = true
				if g.UtilPct != 2 {
					t.Errorf("tick %d: GPU-1 util = %d, want 2 (항상 idle)", i, g.UtilPct)
				}
			case "GPU-2":
				sawGPU2 = true
				if g.UtilPct != 0 {
					t.Errorf("tick %d: GPU-2 util = %d, want 0 (항상 truly-idle)", i, g.UtilPct)
				}
			}
		}
		if !sawGPU1 || !sawGPU2 {
			t.Errorf("tick %d: GPU-1 or GPU-2 누락 (sawGPU1=%v sawGPU2=%v)", i, sawGPU1, sawGPU2)
		}
	}
}

// 결정성: 두 FakeTier 인스턴스가 같은 호출 시퀀스에 같은 결과를 낸다.
// 위 phase 테스트가 통과한다는 사실 자체가 단일 인스턴스 결정성을
// 증명하지만, *상태가 인스턴스에 격리*된다는 보장은 별도. 두 개를
// 같이 굴려 서로 간섭 안 함도 같이 확인.
func TestFakeTier_determinism(t *testing.T) {
	a, b := &FakeTier{}, &FakeTier{}
	ctx := context.Background()
	for i := 0; i < 12; i++ {
		sa, err := a.Collect(ctx, time.Time{})
		if err != nil {
			t.Fatalf("a tick %d: %v", i, err)
		}
		sb, err := b.Collect(ctx, time.Time{})
		if err != nil {
			t.Fatalf("b tick %d: %v", i, err)
		}
		if len(sa.GPUs) != len(sb.GPUs) || len(sa.Procs) != len(sb.Procs) {
			t.Fatalf("tick %d: 길이 불일치 a=%d/%d b=%d/%d",
				i, len(sa.GPUs), len(sa.Procs), len(sb.GPUs), len(sb.Procs))
		}
		for j := range sa.GPUs {
			if sa.GPUs[j] != sb.GPUs[j] {
				t.Errorf("tick %d GPUs[%d]: a=%+v b=%+v", i, j, sa.GPUs[j], sb.GPUs[j])
			}
		}
		for j := range sa.Procs {
			pa, pb := sa.Procs[j], sb.Procs[j]
			if pa.GPUUUID != pb.GPUUUID || pa.PID != pb.PID || pa.MemUsedMB != pb.MemUsedMB {
				t.Errorf("tick %d Procs[%d]: a=%+v b=%+v", i, j, pa, pb)
			}
		}
	}
}
