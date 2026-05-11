package main

import (
	"os"
	"path/filepath"
	"testing"
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
