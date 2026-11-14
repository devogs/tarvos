package prom

import (
	"errors"
	"io"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/devogs/tarvos/telemetry/internal/agent"
)

type fakeSource struct {
	snap *agent.Snapshot
	err  error
}

func (f fakeSource) Snapshot() (*agent.Snapshot, error) { return f.snap, f.err }

func scrape(t *testing.T, src agent.Source) string {
	t.Helper()
	rec := httptest.NewRecorder()
	Handler(src, "2.1.0").ServeHTTP(rec, httptest.NewRequest("GET", "/metrics", nil))
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
