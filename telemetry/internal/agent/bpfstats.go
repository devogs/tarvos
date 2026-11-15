package agent

import (
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"

	"github.com/cilium/ebpf"
)

// BPFStats is the cost of the agent's own eBPF objects.
type BPFStats struct {
	// Maps: memory charged for each map (fdinfo "memlock"), bytes.
	Maps map[string]uint64 `json:"maps"`
	// Programs loaded, by name, with their type.
	Programs []BPFProgram `json:"programs"`
	// AttachedInterfaces: interfaces with the tc programs attached.
	AttachedInterfaces int `json:"attached_interfaces"`
	// RunStats: kernel.bpf_stats_enabled is 1 (set on the host; never toggled here),
	// so RunTimeNS and RunCount are meaningful.
	RunStats bool `json:"run_stats"`
}

// BPFProgram is one loaded program.
type BPFProgram struct {
	Name      string `json:"name"`
	Type      string `json:"type"`
	RunTimeNS uint64 `json:"run_time_ns,omitempty"`
	RunCount  uint64 `json:"run_count,omitempty"`
}

// BPFReporter is implemented by sources that can report their eBPF cost.
type BPFReporter interface {
	BPF() BPFStats
}

// fdinfo reads /proc/self/fdinfo/<fd> into key/value pairs.
func fdinfo(fd int) map[string]string {
	out := map[string]string{}
	data, err := os.ReadFile(fmt.Sprintf("/proc/self/fdinfo/%d", fd))
	if err != nil {
		return out
	}
	for _, line := range strings.Split(string(data), "\n") {
		if k, v, ok := strings.Cut(line, ":"); ok {
			out[strings.TrimSpace(k)] = strings.TrimSpace(v)
		}
	}
	return out
}

func fdUint(info map[string]string, key string) uint64 {
	n, _ := strconv.ParseUint(info[key], 10, 64)
	return n
}

// BPF reports the agent's eBPF maps, programs and attachments.
func (a *Agent) BPF() BPFStats {
	stats := BPFStats{Maps: map[string]uint64{}}
	raw, _ := os.ReadFile("/proc/sys/kernel/bpf_stats_enabled")
	stats.RunStats = strings.TrimSpace(string(raw)) == "1"
	for name, m := range a.coll.Maps {
		if m == nil {
			continue
		}
		stats.Maps[name] = fdUint(fdinfo(m.FD()), "memlock")
	}
	for name, p := range a.coll.Programs {
		if p == nil {
			continue
		}
		prog := BPFProgram{Name: name, Type: programType(p.Type())}
		if stats.RunStats {
			info := fdinfo(p.FD())
			prog.RunTimeNS, prog.RunCount = fdUint(info, "run_time_ns"), fdUint(info, "run_cnt")
		}
		stats.Programs = append(stats.Programs, prog)
	}
	sort.Slice(stats.Programs, func(i, j int) bool { return stats.Programs[i].Name < stats.Programs[j].Name })
	a.mu.Lock()
	stats.AttachedInterfaces = len(a.attached)
	a.mu.Unlock()
	return stats
}

func programType(t ebpf.ProgramType) string {
	switch t {
	case ebpf.SchedCLS:
		return "sched_cls"
	case ebpf.Tracing:
		return "tracing"
	}
	return strings.ToLower(t.String())
}
