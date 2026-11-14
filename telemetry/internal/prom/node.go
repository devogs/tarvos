package prom

import (
	"context"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/vishvananda/netlink"

	"github.com/devogs/tarvos/telemetry/internal/agent"
	"github.com/devogs/tarvos/telemetry/internal/node"
	"github.com/devogs/tarvos/telemetry/internal/state"
)

var (
	ctrCPU = prometheus.NewDesc(namespace+"_container_cpu_seconds_total",
		"CPU time used by the container (its own cgroup).", nil, nil)
	ctrMem = prometheus.NewDesc(namespace+"_container_memory_bytes",
		"Memory of the container's cgroup: usage (memory.current), anon, file, kernel, slab.",
		[]string{"type"}, nil)
	ctrLimit = prometheus.NewDesc(namespace+"_container_memory_limit_bytes",
		"Memory limit of the container's cgroup (absent when unlimited).", nil, nil)
	ctrPIDs = prometheus.NewDesc(namespace+"_container_pids",
		"Processes and threads in the container's cgroup.", nil, nil)
	procRSS = prometheus.NewDesc(namespace+"_process_resident_memory_bytes",
		"Resident memory of a daemon (all its processes).", []string{"process"}, nil)
	procCPU = prometheus.NewDesc(namespace+"_process_cpu_seconds_total",
		"CPU time of a daemon (all its processes, exited ones excluded).", []string{"process"}, nil)
	procCount = prometheus.NewDesc(namespace+"_process_count",
		"Processes of a daemon.", []string{"process"}, nil)
	bpfMapMem = prometheus.NewDesc(namespace+"_ebpf_map_memory_bytes",
		"Memory charged for an eBPF map of the agent.", []string{"map"}, nil)
	bpfProgs = prometheus.NewDesc(namespace+"_ebpf_programs",
		"eBPF programs loaded by the agent, by type.", []string{"type"}, nil)
	bpfAttached = prometheus.NewDesc(namespace+"_ebpf_attached_interfaces",
		"Interfaces with the agent's tc programs attached.", nil, nil)
	bpfRunTime = prometheus.NewDesc(namespace+"_ebpf_program_run_seconds_total",
		"Time spent running an eBPF program (only when the host sets kernel.bpf_stats_enabled=1).",
		[]string{"program"}, nil)
	bpfRuns = prometheus.NewDesc(namespace+"_ebpf_program_runs_total",
		"Runs of an eBPF program (only when the host sets kernel.bpf_stats_enabled=1).",
		[]string{"program"}, nil)
	ifUp = prometheus.NewDesc(namespace+"_interface_up",
		"1 when the interface is operationally up.", []string{"interface", "kind"}, nil)
	ifMTU = prometheus.NewDesc(namespace+"_interface_mtu_bytes",
		"Interface MTU.", []string{"interface"}, nil)
	ifSpeed = prometheus.NewDesc(namespace+"_interface_speed_bits",
		"Link speed in bit/s (absent for virtual interfaces without one).", []string{"interface"}, nil)
	ifErrors = prometheus.NewDesc(namespace+"_interface_netdev_errors_total",
		"Kernel interface error counters.", []string{"interface", "direction"}, nil)
	ifDropped = prometheus.NewDesc(namespace+"_interface_netdev_dropped_total",
		"Kernel interface dropped-packet counters (see also tarvos_interface_drops_total by reason).",
		[]string{"interface", "direction"}, nil)
	nodeUptime = prometheus.NewDesc(namespace+"_node_uptime_seconds",
		"Time since the node (the container's PID 1) started.", nil, nil)
	buildInfo = prometheus.NewDesc(namespace+"_build_info",
		"Tarvos build (value 1).", []string{"version", "frr_version"}, nil)
	scrapeDuration = prometheus.NewDesc(namespace+"_telemetry_scrape_duration_seconds",
		"Time taken to collect this scrape (all sources).", nil, nil)
	snapshotAge = prometheus.NewDesc(namespace+"_telemetry_snapshot_age_seconds",
		"Age of the eBPF counter snapshot of this scrape.", nil, nil)
	stateUp = prometheus.NewDesc(namespace+"_telemetry_state_up",
		"1 when the last read of a state source (routing, kernel) succeeded.", []string{"source"}, nil)
	stateAge = prometheus.NewDesc(namespace+"_telemetry_state_age_seconds",
		"Age of the cached data of a state source (refreshed every PROMETHEUS_STATE_CACHE seconds).",
		[]string{"source"}, nil)
)

var nodeDescs = []*prometheus.Desc{ctrCPU, ctrMem, ctrLimit, ctrPIDs, procRSS, procCPU, procCount,
	bpfMapMem, bpfProgs, bpfAttached, bpfRunTime, bpfRuns, ifUp, ifMTU, ifSpeed, ifErrors, ifDropped,
	nodeUptime, buildInfo, scrapeDuration, snapshotAge, stateUp, stateAge}

// stateDescs are built once from the state package's families.
var stateDescs = func() map[*state.Family]*prometheus.Desc {
	out := map[*state.Family]*prometheus.Desc{}
	for _, f := range state.Families {
		out[f] = prometheus.NewDesc(f.Name, f.Help, f.Labels, nil)
	}
	return out
}()

func gauge(ch chan<- prometheus.Metric, d *prometheus.Desc, v float64, labels ...string) {
	ch <- prometheus.MustNewConstMetric(d, prometheus.GaugeValue, v, labels...)
}

func count(ch chan<- prometheus.Metric, d *prometheus.Desc, v float64, labels ...string) {
	ch <- prometheus.MustNewConstMetric(d, prometheus.CounterValue, v, labels...)
}

// collectNode emits the container, process, eBPF and node series.
func (c *Collector) collectNode(ch chan<- prometheus.Metric) {
	fs := c.FS
	if ctr, err := fs.Container(); err == nil {
		count(ch, ctrCPU, ctr.CPUSeconds)
		for t, v := range ctr.Memory {
			gauge(ch, ctrMem, float64(v), t)
		}
		if ctr.MemoryLimit > 0 {
			gauge(ch, ctrLimit, float64(ctr.MemoryLimit))
		}
		gauge(ch, ctrPIDs, float64(ctr.PIDs))
	}
	if procs, err := fs.Processes(); err == nil {
		for _, p := range procs {
			gauge(ch, procRSS, float64(p.RSSBytes), p.Name)
			count(ch, procCPU, p.CPUSeconds, p.Name)
			gauge(ch, procCount, float64(p.Count), p.Name)
		}
	}
	if up, err := fs.Uptime(); err == nil {
		gauge(ch, nodeUptime, up)
	}
	gauge(ch, buildInfo, 1, c.Version, c.FRRVersion)
	if r, ok := c.Source.(agent.BPFReporter); ok {
		stats := r.BPF()
		for name, mem := range stats.Maps {
			gauge(ch, bpfMapMem, float64(mem), name)
		}
		types := map[string]int{}
		for _, p := range stats.Programs {
			types[p.Type]++
			if stats.RunStats {
				count(ch, bpfRunTime, float64(p.RunTimeNS)/1e9, p.Name)
				count(ch, bpfRuns, float64(p.RunCount), p.Name)
			}
		}
		for t, n := range types {
			gauge(ch, bpfProgs, float64(n), t)
		}
		gauge(ch, bpfAttached, float64(stats.AttachedInterfaces))
	}
}

// collectLinks emits the kernel interface gauges and counters.
func (c *Collector) collectLinks(ch chan<- prometheus.Metric) {
	links, err := netlink.LinkList()
	if err != nil {
		return
	}
	for _, l := range links {
		a := l.Attrs()
		const iffUp, iffLowerUp = 1, 1 << 16
		up := a.OperState == netlink.OperUp ||
			(a.OperState == netlink.OperUnknown && a.RawFlags&iffUp != 0 && a.RawFlags&iffLowerUp != 0)
		gauge(ch, ifUp, boolValue(up), a.Name, l.Type())
		gauge(ch, ifMTU, float64(a.MTU), a.Name)
		if speed := c.speed(a.Name, a.Index); speed > 0 {
			gauge(ch, ifSpeed, float64(speed), a.Name)
		}
		if s := a.Statistics; s != nil {
			count(ch, ifErrors, float64(s.RxErrors), a.Name, "rx")
			count(ch, ifErrors, float64(s.TxErrors), a.Name, "tx")
			count(ch, ifDropped, float64(s.RxDropped), a.Name, "rx")
			count(ch, ifDropped, float64(s.TxDropped), a.Name, "tx")
		}
	}
}

// collectState emits the routing and kernel-feature series (cached, see state.Fetcher).
func (c *Collector) collectState(ch chan<- prometheus.Metric) {
	if c.State == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()
	for _, r := range c.State.Results(ctx) {
		gauge(ch, stateUp, boolValue(r.Err == nil), r.Source)
		gauge(ch, stateAge, time.Since(r.At).Seconds(), r.Source)
		for _, s := range r.Samples {
			d := stateDescs[s.Family]
			if s.Family.Counter {
				count(ch, d, s.Value, s.Labels...)
			} else {
				gauge(ch, d, s.Value, s.Labels...)
			}
		}
	}
}

// speed caches the sysfs link speed per interface for a minute (it changes on
// renegotiation only; reading sysfs for every link on every scrape costs more).
func (c *Collector) speed(name string, index int) uint64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.speeds == nil {
		c.speeds = map[int]speedEntry{}
	}
	if e, ok := c.speeds[index]; ok && e.name == name && time.Since(e.at) < time.Minute {
		return e.bits
	}
	bits := c.FS.Speed(name)
	c.speeds[index] = speedEntry{name: name, bits: bits, at: time.Now()}
	return bits
}

type speedEntry struct {
	name string
	bits uint64
	at   time.Time
}

func boolValue(b bool) float64 {
	if b {
		return 1
	}
	return 0
}

// Ensure node.FS stays the type used here.
var _ = node.FS{}
