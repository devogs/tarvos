// Package node reads the resources of the Tarvos container itself: its own cgroup (CPU,
// memory, PIDs), the CPU and memory of each daemon, kernel interface gauges and the
// node uptime. Everything comes from inside the container (its cgroup and /proc); host
// values (/proc/meminfo, /proc/stat) are never used, so every node reports its own.
package node

import (
	"bufio"
	"bytes"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

// Container resources from the container's cgroup.
type Container struct {
	CgroupVersion int     `json:"cgroup_version"`
	CPUSeconds    float64 `json:"cpu_seconds"`
	// Memory: "usage" (memory.current), and from memory.stat "anon", "file", "kernel",
	// "slab" (cgroup v2; v1 gives usage, anon (rss) and file (cache)).
	Memory      map[string]uint64 `json:"memory"`
	MemoryLimit uint64            `json:"memory_limit"` // 0: unlimited
	PIDs        uint64            `json:"pids"`
}

// FS is the root of the files read (tests use a temporary tree).
type FS struct {
	Root string
}

func (f FS) path(p string) string { return filepath.Join(f.Root, p) }

func (f FS) read(p string) (string, error) {
	data, err := os.ReadFile(f.path(p))
	return strings.TrimSpace(string(data)), err
}

func (f FS) uint(p string) (uint64, error) {
	s, err := f.read(p)
	if err != nil {
		return 0, err
	}
	if s == "max" {
		return 0, nil
	}
	return strconv.ParseUint(s, 10, 64)
}

func (f FS) keyed(p string) (map[string]uint64, error) {
	data, err := os.ReadFile(f.path(p))
	if err != nil {
		return nil, err
	}
	out := map[string]uint64{}
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Fields(line)
		if len(fields) == 2 {
			if n, err := strconv.ParseUint(fields[1], 10, 64); err == nil {
				out[fields[0]] = n
			}
		}
	}
	return out, nil
}

// ownCgroup returns the cgroup v2 directory of this process: with a private cgroup
// namespace (Docker's default on cgroup v2) /proc/self/cgroup says "0::/" and the
// container's cgroup is mounted at /sys/fs/cgroup; with the host namespace it names the
// path below /sys/fs/cgroup. "" when the process is not on cgroup v2.
func (f FS) ownCgroup() string {
	data, err := os.ReadFile(f.path("/proc/self/cgroup"))
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(data), "\n") {
		if rest, ok := strings.CutPrefix(line, "0::"); ok {
			return filepath.Join("/sys/fs/cgroup", rest)
		}
	}
	return ""
}

// Container reads the container's cgroup: v2 (unified) or, as a fallback, v1.
func (f FS) Container() (*Container, error) {
	if dir := f.ownCgroup(); dir != "" {
		if _, err := os.Stat(f.path(filepath.Join(dir, "cpu.stat"))); err == nil {
			return f.v2(dir)
		}
	}
	return f.v1()
}

func (f FS) v2(dir string) (*Container, error) {
	c := &Container{CgroupVersion: 2, Memory: map[string]uint64{}}
	cpu, err := f.keyed(filepath.Join(dir, "cpu.stat"))
	if err != nil {
		return nil, err
	}
	c.CPUSeconds = float64(cpu["usage_usec"]) / 1e6
	if usage, err := f.uint(filepath.Join(dir, "memory.current")); err == nil {
		c.Memory["usage"] = usage
	}
	if stat, err := f.keyed(filepath.Join(dir, "memory.stat")); err == nil {
		for _, k := range []string{"anon", "file", "kernel", "slab"} {
			if v, ok := stat[k]; ok {
				c.Memory[k] = v
			}
		}
	}
	c.MemoryLimit, _ = f.uint(filepath.Join(dir, "memory.max"))
	c.PIDs, _ = f.uint(filepath.Join(dir, "pids.current"))
	return c, nil
}

// v1: the container's controllers are mounted at /sys/fs/cgroup/<controller> (private
// cgroup namespace, Docker's default with cgroup v1 too).
func (f FS) v1() (*Container, error) {
	c := &Container{CgroupVersion: 1, Memory: map[string]uint64{}}
	ns, err := f.uint("/sys/fs/cgroup/cpuacct/cpuacct.usage")
	if err != nil {
		return nil, errors.New("no cgroup v2 or v1 CPU accounting found")
	}
	c.CPUSeconds = float64(ns) / 1e9
	if usage, err := f.uint("/sys/fs/cgroup/memory/memory.usage_in_bytes"); err == nil {
		c.Memory["usage"] = usage
	}
	if stat, err := f.keyed("/sys/fs/cgroup/memory/memory.stat"); err == nil {
		c.Memory["anon"], c.Memory["file"] = stat["rss"], stat["cache"]
	}
	if limit, err := f.uint("/sys/fs/cgroup/memory/memory.limit_in_bytes"); err == nil && limit < 1<<62 {
		c.MemoryLimit = limit
	}
	c.PIDs, _ = f.uint("/sys/fs/cgroup/pids/pids.current")
	return c, nil
}

// Process is one daemon (all its processes: sshd sessions, lldpd's monitor, ...).
type Process struct {
	Name       string  `json:"name"`
	Count      int     `json:"count"`
	RSSBytes   uint64  `json:"rss_bytes"`
	CPUSeconds float64 `json:"cpu_seconds"`
}

// Python daemons and scripts are named after their file (frr_sync.py -> frr_sync).
var pythonDaemons = map[string]bool{
	"frr_sync": true, "netdev_sync": true, "kernel_sync": true, "lldp_sync": true,
}

// Known daemons; any other process is counted as "other" (bounded label set).
var known = map[string]bool{
	"bgpd": true, "zebra": true, "mgmtd": true, "staticd": true, "watchfrr": true,
	"ospfd": true, "ospf6d": true, "isisd": true, "ripd": true, "ripngd": true, "pimd": true,
	"pim6d": true, "ldpd": true, "bfdd": true, "vrrpd": true, "nhrpd": true, "eigrpd": true,
	"babeld": true, "pbrd": true, "fabricd": true, "pathd": true, "sharpd": true,
	"netopeer2-server": true, "sysrepo-plugind": true, "sshd": true, "lldpd": true,
	"rsyslogd": true, "tarvos-telemetry": true, "tini": true, "entrypoint": true,
}

// clockTicks is USER_HZ, 100 on every Linux architecture Tarvos runs on.
const clockTicks = 100

// processName maps /proc/<pid> comm and cmdline to a bounded daemon name.
func processName(comm string, cmdline []string) string {
	switch {
	case comm == "netopeer2-serve": // comm is truncated to 15 characters
		return "netopeer2-server"
	case comm == "sysrepo-plugin":
		return "sysrepo-plugind"
	case comm == "tarvos-telemetr":
		return "tarvos-telemetry"
	case strings.HasPrefix(comm, "python") && len(cmdline) > 1:
		for _, arg := range cmdline[1:] {
			base := strings.TrimSuffix(filepath.Base(arg), ".py")
			if pythonDaemons[base] {
				return base
			}
		}
		return "other"
	case comm == "bash" && len(cmdline) > 1 && strings.HasSuffix(cmdline[1], "entrypoint.sh"):
		return "entrypoint"
	}
	if known[comm] {
		return comm
	}
	return "other"
}

// Processes sums RSS and CPU time per daemon over /proc.
func (f FS) Processes() ([]Process, error) {
	entries, err := os.ReadDir(f.path("/proc"))
	if err != nil {
		return nil, err
	}
	pageSize := uint64(os.Getpagesize())
	byName := map[string]*Process{}
	for _, e := range entries {
		if _, err := strconv.Atoi(e.Name()); err != nil {
			continue
		}
		dir := filepath.Join("/proc", e.Name())
		stat, err := os.ReadFile(f.path(filepath.Join(dir, "stat")))
		if err != nil {
			continue // exited meanwhile
		}
		// pid (comm) state ... : comm may contain spaces and parentheses
		open, closing := bytes.IndexByte(stat, '('), bytes.LastIndexByte(stat, ')')
		if open < 0 || closing < open {
			continue
		}
		comm := string(stat[open+1 : closing])
		fields := strings.Fields(string(stat[closing+1:]))
		if len(fields) < 22 {
			continue
		}
		if fields[0] == "Z" { // zombies hold no memory
			continue
		}
		utime, _ := strconv.ParseUint(fields[11], 10, 64)
		stime, _ := strconv.ParseUint(fields[12], 10, 64)
		rssPages, _ := strconv.ParseUint(fields[21], 10, 64)
		cmdline := []string{comm}
		if strings.HasPrefix(comm, "python") || comm == "bash" { // named after their script
			raw, _ := os.ReadFile(f.path(filepath.Join(dir, "cmdline")))
			cmdline = strings.Split(strings.TrimRight(string(raw), "\x00"), "\x00")
		}
		name := processName(comm, cmdline)
		p := byName[name]
		if p == nil {
			p = &Process{Name: name}
			byName[name] = p
		}
		p.Count++
		p.RSSBytes += rssPages * pageSize
		p.CPUSeconds += float64(utime+stime) / clockTicks
	}
	out := make([]Process, 0, len(byName))
	for _, p := range byName {
		out = append(out, *p)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out, nil
}

// Uptime of the node: time since the container's PID 1 started, in seconds.
func (f FS) Uptime() (float64, error) {
	stat, err := os.ReadFile(f.path("/proc/1/stat"))
	if err != nil {
		return 0, err
	}
	closing := bytes.LastIndexByte(stat, ')')
	fields := strings.Fields(string(stat[closing+1:]))
	if len(fields) < 20 {
		return 0, fmt.Errorf("short /proc/1/stat")
	}
	start, err := strconv.ParseFloat(fields[19], 64)
	if err != nil {
		return 0, err
	}
	up, err := f.read("/proc/uptime")
	if err != nil {
		return 0, err
	}
	var hostUp float64
	if _, err := fmt.Sscanf(up, "%f", &hostUp); err != nil {
		return 0, err
	}
	return max(0, hostUp-start/clockTicks), nil
}

// Link gauges of one kernel interface.
type Link struct {
	Name      string `json:"name"`
	Kind      string `json:"kind,omitempty"`
	Up        bool   `json:"up"` // operationally up (UP and LOWER_UP)
	MTU       int    `json:"mtu"`
	SpeedBits uint64 `json:"speed_bits"` // 0: unknown (virtual devices)
	RXErrors  uint64 `json:"rx_errors"`
	TXErrors  uint64 `json:"tx_errors"`
	RXDropped uint64 `json:"rx_dropped"`
	TXDropped uint64 `json:"tx_dropped"`
}

// Speed reads /sys/class/net/<name>/speed (Mbit/s; -1 or an error for virtual links).
func (f FS) Speed(name string) uint64 {
	s, err := f.read(filepath.Join("/sys/class/net", name, "speed"))
	if err != nil {
		return 0
	}
	n, err := strconv.ParseInt(s, 10, 64)
	if err != nil || n <= 0 {
		return 0
	}
	return uint64(n) * 1_000_000
}

// ParseFdinfo reads the "key:\tvalue" lines of a /proc/<pid>/fdinfo file.
func ParseFdinfo(data []byte) map[string]string {
	out := map[string]string{}
	scanner := bufio.NewScanner(bytes.NewReader(data))
	for scanner.Scan() {
		if k, v, ok := strings.Cut(scanner.Text(), ":"); ok {
			out[strings.TrimSpace(k)] = strings.TrimSpace(v)
		}
	}
	return out
}
