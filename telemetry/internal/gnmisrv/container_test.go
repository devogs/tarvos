package gnmisrv

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/devogs/tarvos/telemetry/internal/agent"
	"github.com/devogs/tarvos/telemetry/internal/node"
)

func TestContainerAndEBPFTrees(t *testing.T) {
	root := t.TempDir()
	for path, content := range map[string]string{
		"proc/self/cgroup":             "0::/\n",
		"sys/fs/cgroup/cpu.stat":       "usage_usec 1500000\n",
		"sys/fs/cgroup/memory.current": "1000\n",
		"sys/fs/cgroup/memory.max":     "max\n",
		"sys/fs/cgroup/pids.current":   "3\n",
		"proc/7/stat":                  "7 (bgpd) S 1 1 1 0 -1 0 0 0 0 0 10 10 0 0 20 0 1 0 5 1 4 0\n",
	} {
		full := filepath.Join(root, path)
		_ = os.MkdirAll(filepath.Dir(full), 0o755)
		if err := os.WriteFile(full, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	c := Container(node.FS{Root: root})
	if c["cpu-seconds"] != 1.5 || c["memory-usage"] != uint64(1000) || c["pids"] != uint64(3) {
		t.Errorf("%v", c)
	}
	if _, ok := c["memory-limit"]; ok {
		t.Error("unlimited memory reported as a limit")
	}
	procs, _ := c["process"].([]any)
	if len(procs) != 1 || procs[0].(map[string]any)["name"] != "bgpd" {
		t.Errorf("processes %v", procs)
	}
	e := EBPF(agent.BPFStats{Maps: map[string]uint64{"if_stats": 4096}, AttachedInterfaces: 2,
		Programs: []agent.BPFProgram{{Name: "tarvos_ingress", Type: "sched_cls"}}})
	if e["attached-interfaces"] != uint32(2) || len(e["map"].([]any)) != 1 {
		t.Errorf("%v", e)
	}
	if _, ok := e["program"].([]any)[0].(map[string]any)["run-count"]; ok {
		t.Error("run statistics without bpf_stats_enabled")
	}
	// Every leaf must convert to a gNMI TypedValue.
	var walk func(v any)
	walk = func(v any) {
		switch x := v.(type) {
		case map[string]any:
			for _, child := range x {
				walk(child)
			}
		case []any:
			for _, child := range x {
				walk(child)
			}
		default:
			if _, err := TypedValue(x); err != nil {
				t.Errorf("leaf %v: %v", x, err)
			}
		}
	}
	walk(c)
	walk(e)
}
