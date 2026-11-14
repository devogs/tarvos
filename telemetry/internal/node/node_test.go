package node

import (
	"os"
	"path/filepath"
	"strconv"
	"testing"
)

func write(t *testing.T, root string, files map[string]string) {
	t.Helper()
	for path, content := range files {
		full := filepath.Join(root, path)
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(full, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

func TestContainerCgroupV2PrivateNamespace(t *testing.T) {
	root := t.TempDir()
	write(t, root, map[string]string{
		"proc/self/cgroup":             "0::/\n",
		"sys/fs/cgroup/cpu.stat":       "usage_usec 2500000\nuser_usec 2000000\n",
		"sys/fs/cgroup/memory.current": "130023424\n",
		"sys/fs/cgroup/memory.stat":    "anon 80000000\nfile 40000000\nkernel 9000000\nslab 5000000\nsock 0\n",
		"sys/fs/cgroup/memory.max":     "max\n",
		"sys/fs/cgroup/pids.current":   "42\n",
	})
	c, err := FS{Root: root}.Container()
	if err != nil {
		t.Fatal(err)
	}
	if c.CgroupVersion != 2 || c.CPUSeconds != 2.5 || c.Memory["usage"] != 130023424 ||
		c.Memory["anon"] != 80000000 || c.Memory["slab"] != 5000000 || c.MemoryLimit != 0 || c.PIDs != 42 {
		t.Errorf("%+v", c)
	}
	if _, ok := c.Memory["sock"]; ok {
		t.Error("unbounded memory types exported")
	}
}

func TestContainerCgroupV2HostNamespaceUsesOwnPath(t *testing.T) {
	root := t.TempDir()
	dir := "sys/fs/cgroup/system.slice/docker-abc.scope/"
	write(t, root, map[string]string{
		"proc/self/cgroup":       "0::/system.slice/docker-abc.scope\n",
		"sys/fs/cgroup/cpu.stat": "usage_usec 999999999999\n", // the host: must not be read
		dir + "cpu.stat":         "usage_usec 1000000\n",
		dir + "memory.current":   "1000\n",
		dir + "memory.max":       "268435456\n",
		dir + "pids.current":     "7\n",
	})
	c, err := FS{Root: root}.Container()
	if err != nil || c.CPUSeconds != 1 || c.MemoryLimit != 268435456 || c.PIDs != 7 {
		t.Errorf("%+v %v", c, err)
	}
}

func TestContainerCgroupV1(t *testing.T) {
	root := t.TempDir()
	write(t, root, map[string]string{
		"proc/self/cgroup":                           "12:memory:/\n",
		"sys/fs/cgroup/cpuacct/cpuacct.usage":        "3000000000\n",
		"sys/fs/cgroup/memory/memory.usage_in_bytes": "5000\n",
		"sys/fs/cgroup/memory/memory.stat":           "cache 1000\nrss 3000\n",
		"sys/fs/cgroup/memory/memory.limit_in_bytes": "9223372036854771712\n",
		"sys/fs/cgroup/pids/pids.current":            "9\n",
	})
	c, err := FS{Root: root}.Container()
	if err != nil || c.CgroupVersion != 1 || c.CPUSeconds != 3 || c.Memory["anon"] != 3000 ||
		c.Memory["file"] != 1000 || c.MemoryLimit != 0 || c.PIDs != 9 {
		t.Errorf("%+v %v", c, err)
	}
}

func stat(pid, comm, state string, utime, stime, rss int) string {
	// pid (comm) state ppid pgrp session tty tpgid flags minflt cminflt majflt cmajflt
	// utime stime cutime cstime priority nice threads itrealvalue starttime vsize rss
	return pid + " (" + comm + ") " + state + " 1 1 1 0 -1 0 0 0 0 0 " +
		itoa(utime) + " " + itoa(stime) + " 0 0 20 0 1 0 500 1000 " + itoa(rss) + " 0\n"
}

func itoa(n int) string { return strconv.Itoa(n) }

func TestProcessesAreGroupedByDaemon(t *testing.T) {
	root := t.TempDir()
	write(t, root, map[string]string{
		"proc/10/stat":    stat("10", "bgpd", "S", 150, 50, 1000),
		"proc/10/cmdline": "/usr/lib/frr/bgpd\x00-d\x00",
		"proc/11/stat":    stat("11", "sshd", "S", 10, 0, 100),
		"proc/12/stat":    stat("12", "sshd", "S", 10, 0, 100),
		"proc/13/stat":    stat("13", "python3", "S", 100, 0, 3000),
		"proc/13/cmdline": "python3\x00/opt/tarvos/daemons/frr_sync.py\x00",
		"proc/14/stat":    stat("14", "netopeer2-serve", "S", 0, 0, 10),
		"proc/15/stat":    stat("15", "sleep", "S", 0, 0, 10),
		"proc/16/stat":    stat("16", "zebra", "Z", 0, 0, 0),
		"proc/17/stat":    stat("17", "weird) (name", "S", 0, 0, 10),
	})
	procs, err := FS{Root: root}.Processes()
	if err != nil {
		t.Fatal(err)
	}
	page := uint64(os.Getpagesize())
	got := map[string]Process{}
	for _, p := range procs {
		got[p.Name] = p
	}
	if p := got["bgpd"]; p.CPUSeconds != 2 || p.RSSBytes != 1000*page || p.Count != 1 {
		t.Errorf("bgpd %+v", p)
	}
	if p := got["sshd"]; p.Count != 2 || p.RSSBytes != 200*page {
		t.Errorf("sshd %+v", p)
	}
	if _, ok := got["frr_sync"]; !ok {
		t.Errorf("python daemon not named: %+v", procs)
	}
	if _, ok := got["netopeer2-server"]; !ok {
		t.Error("truncated comm not mapped")
	}
	if got["other"].Count != 2 {
		t.Errorf("other %+v", got["other"])
	}
	if _, ok := got["zebra"]; ok {
		t.Error("zombie counted")
	}
}

func TestUptime(t *testing.T) {
	root := t.TempDir()
	write(t, root, map[string]string{
		"proc/1/stat": stat("1", "tini", "S", 0, 0, 10), // starttime 500 ticks = 5 s
		"proc/uptime": "65.50 100.00\n",
	})
	up, err := FS{Root: root}.Uptime()
	if err != nil || up != 60.5 {
		t.Errorf("%v %v", up, err)
	}
}
