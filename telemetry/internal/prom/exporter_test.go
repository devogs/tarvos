package prom

import (
	"context"
	"errors"
	"io"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/devogs/tarvos/telemetry/internal/agent"
	"github.com/devogs/tarvos/telemetry/internal/node"
	"github.com/devogs/tarvos/telemetry/internal/state"
)

type fakeSource struct {
	snap *agent.Snapshot
	err  error
}

func (f fakeSource) Snapshot() (*agent.Snapshot, error) { return f.snap, f.err }

func scrape(t *testing.T, src agent.Source) string {
	t.Helper()
	rec := httptest.NewRecorder()
	Handler(&Collector{Source: src, Version: "2.1.0"}).ServeHTTP(rec, httptest.NewRequest("GET", "/metrics", nil))
	body, _ := io.ReadAll(rec.Result().Body)
	return string(body)
}

func TestMetrics(t *testing.T) {
	snap := &agent.Snapshot{
		SizeBuckets: agent.SizeBuckets, Attach: "tcx", DropTracing: true,
		Interfaces: []agent.Interface{
			{Name: "eth1", Instrumented: true,
				RX:          agent.Traffic{Packets: 10, Bytes: 2000, Sizes: []uint64{4, 0, 6, 0, 0, 0, 0, 0}},
				TX:          agent.Traffic{Packets: 1, Bytes: 60, Sizes: []uint64{1, 0, 0, 0, 0, 0, 0, 0}},
				DropReasons: map[string]uint64{"no_socket": 3}},
			{Name: "mgmt", DropReasons: map[string]uint64{"netfilter_drop": 2}},
		},
		VNIs: []agent.VNI{{VNI: 100, Interface: "vx100", RX: agent.Counter{Packets: 7, Bytes: 700}}},
		ControlPlane: []agent.ControlPlane{
			{Interface: "eth1", Protocol: "bgp", Direction: "rx", Counter: agent.Counter{Packets: 5, Bytes: 400}},
		},
	}
	body := scrape(t, fakeSource{snap: snap})
	for _, want := range []string{
		`tarvos_telemetry_up 1`,
		`tarvos_telemetry_info{attach="tcx",drop_tracing="true",version="2.1.0"} 1`,
		`tarvos_interface_packets_total{direction="rx",interface="eth1"} 10`,
		`tarvos_interface_bytes_total{direction="tx",interface="eth1"} 60`,
		`tarvos_interface_packet_size_bytes_bucket{direction="rx",interface="eth1",le="64"} 4`,
		`tarvos_interface_packet_size_bytes_bucket{direction="rx",interface="eth1",le="256"} 10`,
		`tarvos_interface_packet_size_bytes_bucket{direction="rx",interface="eth1",le="+Inf"} 10`,
		`tarvos_interface_packet_size_bytes_sum{direction="rx",interface="eth1"} 2000`,
		`tarvos_interface_drops_total{interface="eth1",reason="no_socket"} 3`,
		`tarvos_interface_drops_total{interface="mgmt",reason="netfilter_drop"} 2`,
		`tarvos_vni_packets_total{direction="rx",interface="vx100",vni="100"} 7`,
		`tarvos_control_plane_packets_total{direction="rx",interface="eth1",protocol="bgp"} 5`,
	} {
		if !strings.Contains(body, want) {
			t.Errorf("missing %s", want)
		}
	}
	// Not instrumented: no traffic series, only drops.
	if strings.Contains(body, `tarvos_interface_packets_total{direction="rx",interface="mgmt"}`) {
		t.Error("traffic series for a non-instrumented interface")
	}
}

func TestAgentFailure(t *testing.T) {
	body := scrape(t, fakeSource{err: errors.New("down")})
	if !strings.Contains(body, "tarvos_telemetry_up 0") {
		t.Error(body)
	}
}

func TestNodeAndStateSeries(t *testing.T) {
	root := t.TempDir()
	for path, content := range map[string]string{
		"proc/self/cgroup":             "0::/\n",
		"sys/fs/cgroup/cpu.stat":       "usage_usec 4000000\n",
		"sys/fs/cgroup/memory.current": "131072000\n",
		"sys/fs/cgroup/memory.stat":    "anon 1000\nfile 2000\nkernel 3000\nslab 400\n",
		"sys/fs/cgroup/memory.max":     "536870912\n",
		"sys/fs/cgroup/pids.current":   "40\n",
		"proc/1/stat":                  "1 (tini) S 0 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 100 1000 10 0\n",
		"proc/1/cmdline":               "/usr/bin/tini\x00",
		"proc/uptime":                  "11.00 1.00\n",
	} {
		full := filepath.Join(root, path)
		_ = os.MkdirAll(filepath.Dir(full), 0o755)
		if err := os.WriteFile(full, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	routing, _ := os.ReadFile("../state/testdata/routing.json")
	c := &Collector{
		Source: fakeSource{snap: &agent.Snapshot{Time: time.Now()}}, Version: "2.1.1", FRRVersion: "10.7.1",
		FS: node.FS{Root: root}, Node: true,
		State: &state.Fetcher{Routing: true, TTL: time.Minute,
			Read: func(context.Context, string) ([]byte, error) { return routing, nil }},
	}
	rec := httptest.NewRecorder()
	Handler(c).ServeHTTP(rec, httptest.NewRequest("GET", "/metrics", nil))
	body, _ := io.ReadAll(rec.Result().Body)
	for _, want := range []string{
		`tarvos_container_cpu_seconds_total 4`,
		`tarvos_container_memory_bytes{type="usage"} 1.31072e+08`,
		`tarvos_container_memory_limit_bytes 5.36870912e+08`,
		`tarvos_container_pids 40`,
		`tarvos_process_count{process="tini"} 1`,
		`tarvos_node_uptime_seconds 10`,
		`tarvos_build_info{frr_version="10.7.1",version="2.1.1"} 1`,
		`tarvos_telemetry_state_up{source="routing"} 1`,
		`tarvos_bgp_neighbor_up{neighbor="172.30.0.12",remote_as="65000",vrf="default"} 1`,
		`tarvos_evpn_routes{route_type="3"} 2`,
		`tarvos_telemetry_scrape_duration_seconds`,
		`tarvos_telemetry_snapshot_age_seconds`,
	} {
		if !strings.Contains(string(body), want) {
			t.Errorf("missing %s", want)
		}
	}
	if rec.Code != 200 {
		t.Errorf("status %d: %s", rec.Code, body)
	}
}

func TestSwitchesKeepTheEBPFOnlyFootprint(t *testing.T) {
	// PROMETHEUS_KERNEL_METRICS=no and PROMETHEUS_ROUTING_METRICS=no: Node false, no State.
	body := scrape(t, fakeSource{snap: &agent.Snapshot{Time: time.Now()}})
	for _, prefix := range []string{"tarvos_container_", "tarvos_process_", "tarvos_interface_up",
		"tarvos_bgp_", "tarvos_evpn_", "tarvos_qos_", "tarvos_copp_", "tarvos_telemetry_state_"} {
		for _, line := range strings.Split(body, "\n") {
			if strings.HasPrefix(line, prefix) {
				t.Errorf("eBPF-only exporter emits %s", line)
			}
		}
	}
	if !strings.Contains(body, "tarvos_telemetry_up 1") {
		t.Error(body)
	}
}
