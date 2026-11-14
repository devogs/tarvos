// Package state turns the operational data that NETCONF and gNMI already serve (BGP,
// EVPN, FRR daemons, QoS, CoPP, AAA, LLDP) into Prometheus samples. The data is read
// from the Sysrepo operational datastore, so it is computed in one place (the Tarvos
// backends) and Prometheus, NETCONF and gNMI always agree.
//
// Label sets are fixed and bounded: no per-MAC or per-route label (EVPN routes are
// counted per route type; the routes themselves stay in NETCONF/gNMI).
package state

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os/exec"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Family describes one metric family.
type Family struct {
	Name    string
	Help    string
	Counter bool
	Labels  []string
}

// Sample is one value of a family.
type Sample struct {
	Family *Family
	Labels []string
	Value  float64
}

func fam(name, help string, counter bool, labels ...string) *Family {
	f := &Family{Name: "tarvos_" + name, Help: help, Counter: counter, Labels: labels}
	Families = append(Families, f)
	return f
}

// Families lists every family this package can emit (for the README table and tests).
var Families []*Family

// Routing families.
var (
	bgpNeighbors = fam("bgp_neighbors", "BGP neighbours per instance, configured or established.", false, "vrf", "state")
	bgpUp        = fam("bgp_neighbor_up", "1 while the BGP session is established.", false, "vrf", "neighbor", "remote_as")
	bgpUptime    = fam("bgp_neighbor_uptime_seconds", "Time since the session was established.", false, "vrf", "neighbor")
	bgpPfxRcvd   = fam("bgp_prefixes_received", "Prefixes received, per address family.", false, "vrf", "neighbor", "afi_safi")
	bgpPfxSent   = fam("bgp_prefixes_sent", "Prefixes sent, per address family.", false, "vrf", "neighbor", "afi_safi")
	bgpMessages  = fam("bgp_messages_total", "BGP messages (type: all, update, notification).", true, "vrf", "neighbor", "direction", "type")
	bgpResets    = fam("bgp_session_resets_total", "Times the session went down (connections dropped).", true, "vrf", "neighbor")
	bgpEstab     = fam("bgp_session_established_total", "Times the session was established.", true, "vrf", "neighbor")
	evpnRoutes   = fam("evpn_routes", "EVPN routes per route type (1-5).", false, "route_type")
	evpnVNI      = fam("evpn_vni_info", "EVPN VNI (value 1): type l2 or l3, tenant VRF, VXLAN device.", false, "vni", "type", "vrf", "interface")
	evpnMACs     = fam("evpn_vni_macs", "MAC addresses known in a VNI.", false, "vni")
	evpnARP      = fam("evpn_vni_arp_nd", "ARP/ND entries known in a VNI.", false, "vni")
	evpnVTEPs    = fam("evpn_vni_remote_vteps", "Remote VTEPs of a VNI.", false, "vni")
	pimNeighbors = fam("pim_neighbors", "PIM neighbours per interface (while pimd runs).", false, "interface")
	ldpUp        = fam("ldp_neighbor_up", "1 while the LDP session is operational (while ldpd runs).", false, "lsr_id")
	vrrpMaster   = fam("vrrp_master", "1 when this router is VRRP master, 0 backup or initialising.", false, "interface", "vrid", "afi")
	vrrpTrans    = fam("vrrp_transitions_total", "VRRP state transitions.", true, "interface", "vrid", "afi")
	bfdUp        = fam("bfd_peer_up", "1 while the BFD session is up (while bfdd runs).", false, "vrf", "peer")
)

// Kernel feature families.
var (
	qosClassPkts  = fam("qos_class_packets_total", "Packets sent by a QoS class (class: id or default).", true, "interface", "class")
	qosClassBytes = fam("qos_class_bytes_total", "Bytes sent by a QoS class.", true, "interface", "class")
	qosClassDrops = fam("qos_class_drops_total", "Packets dropped by a QoS class.", true, "interface", "class")
	qosClassOver  = fam("qos_class_overlimits_total", "Times a QoS class hit its rate limit.", true, "interface", "class")
	qosRootPkts   = fam("qos_qdisc_packets_total", "Packets sent by the root qdisc.", true, "interface", "kind")
	qosRootBytes  = fam("qos_qdisc_bytes_total", "Bytes sent by the root qdisc.", true, "interface", "kind")
	qosRootDrops  = fam("qos_qdisc_drops_total", "Packets dropped by the root qdisc.", true, "interface", "kind")
	qosRootOver   = fam("qos_qdisc_overlimits_total", "Overlimits of the root qdisc.", true, "interface", "kind")
	coppActive    = fam("copp_active", "1 while the Tarvos CoPP table is installed.", false)
	coppMatchP    = fam("copp_matched_packets_total", "Packets matched by a CoPP rule.", true, "rule")
	coppMatchB    = fam("copp_matched_bytes_total", "Bytes matched by a CoPP rule.", true, "rule")
	coppDropP     = fam("copp_dropped_packets_total", "Packets dropped by a CoPP rule.", true, "rule")
	coppDropB     = fam("copp_dropped_bytes_total", "Bytes dropped by a CoPP rule.", true, "rule")
	aaaReachable  = fam("aaa_server_reachable", "1 when the TACACS+ server answered the last probe.", false, "server")
	aaaResponse   = fam("aaa_server_response_seconds", "TCP connect time of the last TACACS+ probe.", false, "server")
	aaaAuth       = fam("aaa_authentications_total", "Authentications by method (tacacs, local) and result.", true, "method", "result")
	lldpNeighbors = fam("lldp_neighbors", "LLDP neighbours per local interface.", false, "interface")
)

// XPaths of the trees read (one sysrepocfg call per group: XPath unions are allowed).
const (
	RoutingXPath = "/tarvos-bgp:bgp/state/instance | /tarvos-bgp:bgp/state/evpn/vni | " +
		"/tarvos-bgp:bgp/state/evpn/route-count | /tarvos-frr:frr/state"
	KernelXPath = "/tarvos-qos:qos | /tarvos-nftables:nftables | /tarvos-aaa:aaa/state | " +
		"/tarvos-lldp:lldp/neighbors"
)

// Fetcher reads the operational datastore with sysrepocfg and caches the samples for
// TTL: a scrape every second costs one read per TTL (the FRR "show ... json" calls
// behind the BGP state are the expensive part).
type Fetcher struct {
	Sysrepocfg string
	TTL        time.Duration
	Routing    bool
	Kernel     bool
	// Read returns the RFC 7951 JSON of an XPath (tests replace it).
	Read func(ctx context.Context, xpath string) ([]byte, error)

	mu    sync.Mutex
	cache map[string]*Result
}

// Result of one source ("routing" or "kernel").
type Result struct {
	Source  string
	Samples []Sample
	At      time.Time
	Err     error
}

func (f *Fetcher) read(ctx context.Context, xpath string) ([]byte, error) {
	if f.Read != nil {
		return f.Read(ctx, xpath)
	}
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, f.Sysrepocfg, "--export", "--datastore", "operational",
		"--format", "json", "--xpath", xpath)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("sysrepocfg %s: %v %s", xpath, err, strings.TrimSpace(stderr.String()))
	}
	return out, nil
}

// Results returns one result per enabled source, refreshed when older than TTL. A
// source whose last refresh failed has no samples (stale data is never served as
// fresh) and its error set.
func (f *Fetcher) Results(ctx context.Context) []Result {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.cache == nil {
		f.cache = map[string]*Result{}
	}
	var out []Result
	for _, src := range []struct {
		on    bool
		name  string
		xpath string
		parse func(map[string]any) []Sample
	}{{f.Routing, "routing", RoutingXPath, Routing}, {f.Kernel, "kernel", KernelXPath, Kernel}} {
		if !src.on {
			continue
		}
		r := f.cache[src.name]
		if r == nil || time.Since(r.At) >= f.TTL {
			r = &Result{Source: src.name, At: time.Now()}
			r.Samples, r.Err = f.fetch(ctx, src.xpath, src.parse)
			if r.Err != nil {
				r.Samples = nil
			}
			f.cache[src.name] = r
		}
		out = append(out, *r)
	}
	return out
}

func (f *Fetcher) fetch(ctx context.Context, xpath string, parse func(map[string]any) []Sample) ([]Sample, error) {
	raw, err := f.read(ctx, xpath)
	if err != nil {
		return nil, err
	}
	doc := map[string]any{}
	if len(bytes.TrimSpace(raw)) > 0 {
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.UseNumber()
		if err := decoder.Decode(&doc); err != nil {
			return nil, fmt.Errorf("decoding %s: %w", xpath, err)
		}
	}
	return parse(doc), nil
}

// --- JSON helpers -------------------------------------------------------------------

func obj(v any, path ...string) map[string]any {
	for _, p := range path {
		m, _ := v.(map[string]any)
		v = m[p]
	}
	m, _ := v.(map[string]any)
	return m
}

func list(v any, path ...string) []map[string]any {
	for _, p := range path {
		m, _ := v.(map[string]any)
		v = m[p]
	}
	items, _ := v.([]any)
	out := make([]map[string]any, 0, len(items))
	for _, item := range items {
		if m, ok := item.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out
}

func str(m map[string]any, key string) string {
	switch v := m[key].(type) {
	case string:
		return v
	case json.Number:
		return v.String()
	case bool:
		return strconv.FormatBool(v)
	}
	return ""
}

// num reads a number; RFC 7951 encodes 64-bit integers as strings.
func num(m map[string]any, key string) (float64, bool) {
	switch v := m[key].(type) {
	case json.Number:
		f, err := v.Float64()
		return f, err == nil
	case string:
		f, err := strconv.ParseFloat(v, 64)
		return f, err == nil
	case bool:
		if v {
			return 1, true
		}
		return 0, true
	}
	return 0, false
}

type builder struct{ out []Sample }

func (b *builder) add(f *Family, v float64, labels ...string) {
	b.out = append(b.out, Sample{Family: f, Labels: labels, Value: v})
}

func (b *builder) num(f *Family, m map[string]any, key string, labels ...string) {
	if v, ok := num(m, key); ok {
		b.add(f, v, labels...)
	}
}

func bool01(ok bool) float64 {
	if ok {
		return 1
	}
	return 0
}

// --- routing ------------------------------------------------------------------------

// Routing builds the routing samples from the JSON of RoutingXPath.
func Routing(doc map[string]any) []Sample {
	b := &builder{}
	state := obj(doc, "tarvos-bgp:bgp", "state")
	for _, inst := range list(state, "instance") {
		vrf := str(inst, "vrf")
		b.num(bgpNeighbors, inst, "configured-neighbors", vrf, "configured")
		b.num(bgpNeighbors, inst, "established-neighbors", vrf, "established")
		for _, n := range list(inst, "neighbor") {
			addr := str(n, "address")
			established := str(n, "session-state") == "established"
			b.add(bgpUp, bool01(established), vrf, addr, str(n, "remote-as"))
			if established {
				b.num(bgpUptime, n, "uptime", vrf, addr)
			}
			b.num(bgpResets, n, "connections-dropped", vrf, addr)
			b.num(bgpEstab, n, "connections-established", vrf, addr)
			msgs := obj(n, "messages")
			for _, m := range []struct{ key, dir, kind string }{
				{"received", "rx", "all"}, {"sent", "tx", "all"},
				{"updates-received", "rx", "update"}, {"updates-sent", "tx", "update"},
				{"notifications-received", "rx", "notification"}, {"notifications-sent", "tx", "notification"},
			} {
				b.num(bgpMessages, msgs, m.key, vrf, addr, m.dir, m.kind)
			}
			for _, af := range list(n, "address-family") {
				b.num(bgpPfxRcvd, af, "prefixes-received", vrf, addr, str(af, "name"))
				b.num(bgpPfxSent, af, "prefixes-sent", vrf, addr, str(af, "name"))
			}
		}
	}
	evpn := obj(state, "evpn")
	for _, rc := range list(evpn, "route-count") {
		b.num(evpnRoutes, rc, "count", str(rc, "route-type"))
	}
	for _, v := range list(evpn, "vni") {
		vni := str(v, "vni")
		b.add(evpnVNI, 1, vni, str(v, "type"), str(v, "tenant-vrf"), str(v, "vxlan-interface"))
		b.num(evpnMACs, v, "mac-count", vni)
		b.num(evpnARP, v, "arp-nd-count", vni)
		vteps, _ := v["remote-vtep"].([]any)
		b.add(evpnVTEPs, float64(len(vteps)), vni)
	}
	frr := obj(doc, "tarvos-frr:frr", "state")
	pim := map[string]int{}
	for _, n := range list(frr, "pim", "neighbor") {
		pim[str(n, "interface")]++
	}
	for _, iface := range sortedKeys(pim) {
		b.add(pimNeighbors, float64(pim[iface]), iface)
	}
	for _, n := range list(frr, "ldp", "neighbor") {
		b.add(ldpUp, bool01(strings.EqualFold(str(n, "state"), "operational")), str(n, "lsr-id"))
	}
	for _, r := range list(frr, "vrrp", "router") {
		for _, af := range list(r, "address-family") {
			labels := []string{str(r, "interface"), str(r, "vrid"), str(af, "name")}
			b.add(vrrpMaster, bool01(str(af, "state") == "master"), labels...)
			b.num(vrrpTrans, af, "transitions", labels...)
		}
	}
	for _, p := range list(frr, "bfd", "peer") {
		vrf := str(p, "vrf")
		if vrf == "" {
			vrf = "default"
		}
		b.add(bfdUp, bool01(str(p, "status") == "up"), vrf, str(p, "peer-address"))
	}
	return b.out
}

// --- kernel features ----------------------------------------------------------------

// Kernel builds the QoS, CoPP, AAA and LLDP samples from the JSON of KernelXPath.
func Kernel(doc map[string]any) []Sample {
	b := &builder{}
	for _, iface := range list(doc, "tarvos-qos:qos", "interface") {
		name := str(iface, "name")
		st := obj(iface, "state")
		if root := obj(st, "root-qdisc"); root != nil {
			kind := str(root, "kind")
			b.num(qosRootPkts, root, "packets", name, kind)
			b.num(qosRootBytes, root, "bytes", name, kind)
			b.num(qosRootDrops, root, "drops", name, kind)
			b.num(qosRootOver, root, "overlimits", name, kind)
		}
		classes := []struct {
			id string
			st map[string]any
		}{}
		for _, c := range list(iface, "class") {
			if cs := obj(c, "state"); cs != nil {
				classes = append(classes, struct {
					id string
					st map[string]any
				}{str(c, "id"), cs})
			}
		}
		if d := obj(st, "default-class"); d != nil {
			classes = append(classes, struct {
				id string
				st map[string]any
			}{"default", d})
		}
		for _, c := range classes {
			b.num(qosClassPkts, c.st, "packets", name, c.id)
			b.num(qosClassBytes, c.st, "bytes", name, c.id)
			b.num(qosClassDrops, c.st, "drops", name, c.id)
			b.num(qosClassOver, c.st, "overlimits", name, c.id)
		}
	}
	copp := obj(doc, "tarvos-nftables:nftables", "copp")
	if st := obj(copp, "state"); st != nil {
		b.num(coppActive, st, "active")
	}
	for _, r := range list(copp, "rule") {
		st, name := obj(r, "state"), str(r, "name")
		if st == nil {
			continue
		}
		b.num(coppMatchP, st, "matched-packets", name)
		b.num(coppMatchB, st, "matched-bytes", name)
		b.num(coppDropP, st, "dropped-packets", name)
		b.num(coppDropB, st, "dropped-bytes", name)
	}
	aaa := obj(doc, "tarvos-aaa:aaa", "state")
	for _, s := range list(aaa, "server") {
		addr := str(s, "address")
		b.num(aaaReachable, s, "reachable", addr)
		if ms, ok := num(s, "response-time"); ok {
			b.add(aaaResponse, ms/1000, addr)
		}
	}
	for _, a := range list(aaa, "authentication-count") {
		b.num(aaaAuth, a, "count", str(a, "method"), str(a, "result"))
	}
	lldp := map[string]int{}
	for _, n := range list(doc, "tarvos-lldp:lldp", "neighbors", "neighbor") {
		lldp[str(n, "interface")]++
	}
	for _, iface := range sortedKeys(lldp) {
		b.add(lldpNeighbors, float64(lldp[iface]), iface)
	}
	return b.out
}

func sortedKeys(m map[string]int) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
