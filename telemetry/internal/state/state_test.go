package state

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"testing"
	"time"
)

func load(t *testing.T, name string) map[string]any {
	t.Helper()
	raw, err := os.ReadFile("testdata/" + name)
	if err != nil {
		t.Fatal(err)
	}
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.UseNumber()
	doc := map[string]any{}
	if err := decoder.Decode(&doc); err != nil {
		t.Fatal(err)
	}
	return doc
}

// index renders samples as "name{v1,v2} value" for easy assertions.
func index(samples []Sample) map[string]float64 {
	out := map[string]float64{}
	for _, s := range samples {
		key := s.Family.Name + "{" + strings.Join(s.Labels, ",") + "}"
		if _, dup := out[key]; dup {
			panic("duplicate series " + key)
		}
		if len(s.Labels) != len(s.Family.Labels) {
			panic("label count mismatch for " + key)
		}
		out[key] = s.Value
	}
	return out
}

func TestRoutingFromTheLabState(t *testing.T) {
	got := index(Routing(load(t, "routing.json")))
	for key, want := range map[string]float64{
		"tarvos_bgp_neighbor_up{default,172.30.0.12,65000}":            1,
		"tarvos_bgp_neighbor_uptime_seconds{default,172.30.0.12}":      18,
		"tarvos_bgp_neighbors{default,established}":                    1,
		"tarvos_bgp_neighbors{RED,configured}":                         0,
		"tarvos_bgp_prefixes_received{default,172.30.0.12,l2vpn-evpn}": 7,
		"tarvos_bgp_prefixes_sent{default,172.30.0.12,ipv4-unicast}":   1,
		"tarvos_bgp_messages_total{default,172.30.0.12,rx,update}":     8,
		"tarvos_bgp_messages_total{default,172.30.0.12,tx,all}":        8,
		"tarvos_bgp_session_resets_total{default,172.30.0.12}":         0,
		"tarvos_evpn_routes{2}":                                        4,
		"tarvos_evpn_routes{5}":                                        4,
		"tarvos_evpn_vni_info{100,l2,RED,vx100}":                       1,
		"tarvos_evpn_vni_macs{100}":                                    3,
		"tarvos_evpn_vni_remote_vteps{100}":                            1,
		"tarvos_evpn_vni_remote_vteps{5000}":                           0,
	} {
		if v, ok := got[key]; !ok || v != want {
			t.Errorf("%s = %v (present %v), want %v", key, v, ok, want)
		}
	}
	for key := range got {
		if strings.Contains(key, ":") && strings.Contains(key, "evpn_route") {
			t.Errorf("per-route label leaked: %s", key)
		}
	}
}

func TestFRRDaemonState(t *testing.T) {
	doc := map[string]any{"tarvos-frr:frr": map[string]any{"state": map[string]any{
		"pim": map[string]any{"neighbor": []any{
			map[string]any{"interface": "eth1", "address": "10.0.0.2"},
			map[string]any{"interface": "eth1", "address": "10.0.0.3"},
		}},
		"ldp": map[string]any{"neighbor": []any{map[string]any{"lsr-id": "1.1.1.1", "state": "OPERATIONAL"}}},
		"vrrp": map[string]any{"router": []any{map[string]any{"interface": "eth1", "vrid": json.Number("5"),
			"address-family": []any{map[string]any{"name": "ipv4", "state": "master", "transitions": json.Number("2")}}}}},
		"bfd": map[string]any{"peer": []any{map[string]any{"peer-address": "10.0.0.2", "status": "down"}}},
	}}}
	got := index(Routing(doc))
	for key, want := range map[string]float64{
		"tarvos_pim_neighbors{eth1}":                 2,
		"tarvos_ldp_neighbor_up{1.1.1.1}":            1,
		"tarvos_vrrp_master{eth1,5,ipv4}":            1,
		"tarvos_vrrp_transitions_total{eth1,5,ipv4}": 2,
		"tarvos_bfd_peer_up{default,10.0.0.2}":       0,
	} {
		if got[key] != want {
			t.Errorf("%s = %v, want %v", key, got[key], want)
		}
	}
}

func TestKernelFeatures(t *testing.T) {
	got := index(Kernel(load(t, "kernel.json")))
	for key, want := range map[string]float64{
		"tarvos_qos_class_packets_total{eth1,10}":          100,
		"tarvos_qos_class_drops_total{eth1,20}":            2,
		"tarvos_qos_class_packets_total{eth1,default}":     10,
		"tarvos_qos_qdisc_bytes_total{eth1,htb}":           25000,
		"tarvos_copp_active{}":                             1,
		"tarvos_copp_dropped_packets_total{icmp}":          195,
		"tarvos_aaa_server_reachable{192.0.2.10}":          0,
		"tarvos_aaa_server_response_seconds{192.0.2.11}":   0.004,
		"tarvos_aaa_authentications_total{tacacs,success}": 7,
		"tarvos_lldp_neighbors{eth1}":                      2,
		"tarvos_lldp_neighbors{eth2}":                      1,
	} {
		if got[key] != want {
			t.Errorf("%s = %v, want %v", key, got[key], want)
		}
	}
	if _, ok := got["tarvos_aaa_server_response_seconds{192.0.2.10}"]; ok {
		t.Error("response time of an unreachable server")
	}
}

func TestFetcherCachesAndNeverServesStaleData(t *testing.T) {
	reads := 0
	fail := false
	f := &Fetcher{Routing: true, TTL: time.Hour, Read: func(_ context.Context, xpath string) ([]byte, error) {
		reads++
		if fail {
			return nil, errors.New("sysrepo down")
		}
		return os.ReadFile("testdata/routing.json")
	}}
	for i := 0; i < 5; i++ {
		r := f.Results(context.Background())
		if len(r) != 1 || r[0].Err != nil || len(r[0].Samples) == 0 {
			t.Fatalf("results %+v", r)
		}
	}
	if reads != 1 {
		t.Errorf("%d reads for 5 scrapes within the TTL", reads)
	}
	fail, f.TTL = true, 0
	r := f.Results(context.Background())
	if r[0].Err == nil || r[0].Samples != nil {
		t.Errorf("failed refresh still serves data: %+v", r[0])
	}
}
