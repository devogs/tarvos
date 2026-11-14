package agent

import (
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"runtime/debug"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/cilium/ebpf"
	"github.com/cilium/ebpf/btf"
	"github.com/cilium/ebpf/link"
	"github.com/cilium/ebpf/rlimit"
	"github.com/vishvananda/netlink"
	"golang.org/x/sys/unix"

	"github.com/devogs/tarvos/telemetry/internal/bpf"
)

// Config of the agent.
type Config struct {
	// Exclude lists interface name globs that are never instrumented ("lo" by default).
	Exclude []string
}

// Agent loads the eBPF programs, attaches them to every interface of the network
// namespace and reads the maps.
type Agent struct {
	cfg      Config
	coll     *ebpf.Collection
	ingress  *ebpf.Program
	egress   *ebpf.Program
	ifStats  *ebpf.Map
	cpStats  *ebpf.Map
	drops    *ebpf.Map
	dropLink link.Link
	reasons  map[uint32]string

	mu       sync.Mutex
	attached map[int]attachment // ifindex -> hooks
	mode     string             // tcx | tc
}

type attachment struct {
	name  string
	close func()
}

// New loads the programs. Drop tracing (tp_btf/kfree_skb, needs CAP_PERFMON and kernel
// BTF) is optional: without it the traffic counters still work.
func New(cfg Config) (*Agent, error) {
	if err := rlimit.RemoveMemlock(); err != nil {
		log.Printf("WARNING: memlock limit not removed (%v); continuing", err)
	}
	spec, err := bpf.LoadTarvos()
	if err != nil {
		return nil, fmt.Errorf("parsing eBPF object: %w", err)
	}
	var netns uint64
	if st, err := os.Stat("/proc/self/ns/net"); err == nil {
		netns = st.Sys().(*syscall.Stat_t).Ino
	}
	if v, ok := spec.Variables["target_netns"]; ok {
		if err := v.Set(uint32(netns)); err != nil {
			return nil, fmt.Errorf("setting target_netns: %w", err)
		}
	}
	coll, err := ebpf.NewCollection(spec)
	dropTracing := true
	if err != nil {
		log.Printf("WARNING: drop tracing unavailable (%v)", err)
		delete(spec.Programs, "tarvos_kfree_skb")
		dropTracing = false
		if coll, err = ebpf.NewCollection(spec); err != nil {
			return nil, fmt.Errorf("loading eBPF programs: %w", err)
		}
	}
	a := &Agent{
		cfg:      cfg,
		coll:     coll,
		ingress:  coll.Programs["tarvos_ingress"],
		egress:   coll.Programs["tarvos_egress"],
		ifStats:  coll.Maps["if_stats"],
		cpStats:  coll.Maps["cp_stats"],
		drops:    coll.Maps["drop_stats"],
		attached: map[int]attachment{},
		reasons:  dropReasons(),
	}
	if dropTracing {
		l, err := link.AttachTracing(link.TracingOptions{Program: coll.Programs["tarvos_kfree_skb"]})
		if err != nil {
			log.Printf("WARNING: drop tracing unavailable (%v)", err)
		} else {
			a.dropLink = l
		}
	}
	// The kernel BTF (tens of MB parsed) is only needed for loading: drop it.
	btf.FlushKernelSpec()
	debug.FreeOSMemory()
	return a, nil
}

// dropReasons reads enum skb_drop_reason from the kernel BTF (names differ between
// kernels, so they are never hardcoded).
func dropReasons() map[uint32]string {
	out := map[uint32]string{}
	spec, err := btf.LoadKernelSpec()
	if err != nil {
		return out
	}
	var enum *btf.Enum
	if err := spec.TypeByName("skb_drop_reason", &enum); err != nil {
		return out
	}
	for _, v := range enum.Values {
		out[uint32(v.Value)] = strings.ToLower(strings.TrimPrefix(v.Name, "SKB_DROP_REASON_"))
	}
	return out
}

// Close detaches everything.
func (a *Agent) Close() {
	a.mu.Lock()
	defer a.mu.Unlock()
	for idx, at := range a.attached {
		at.close()
		delete(a.attached, idx)
	}
	if a.dropLink != nil {
		a.dropLink.Close()
	}
	a.coll.Close()
}

func (a *Agent) excluded(name string) bool {
	for _, pattern := range a.cfg.Exclude {
		if ok, _ := filepath.Match(pattern, name); ok {
			return true
		}
	}
	return false
}

// Instrumented tells whether the agent attaches to a link: not excluded, and with an
// Ethernet header (VRF masters are L3 devices; their traffic is that of their ports).
func Instrumented(name, kind string, exclude func(string) bool) bool {
	return kind != "vrf" && !exclude(name)
}

// Sync attaches to new interfaces and forgets removed ones. Called periodically:
// containerlab and GNS3 attach links after the container started.
func (a *Agent) Sync() error {
	links, err := netlink.LinkList()
	if err != nil {
		return fmt.Errorf("listing links: %w", err)
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	present := map[int]bool{}
	for _, l := range links {
		attrs := l.Attrs()
		if !Instrumented(attrs.Name, l.Type(), a.excluded) {
			continue
		}
		present[attrs.Index] = true
		if at, ok := a.attached[attrs.Index]; ok && at.name == attrs.Name {
			continue
		}
		at, err := a.attach(attrs.Index, attrs.Name)
		if err != nil {
			log.Printf("WARNING: %s: %v", attrs.Name, err)
			continue
		}
		a.attached[attrs.Index] = at
		log.Printf("instrumented %s (%s)", attrs.Name, a.mode)
	}
	for idx, at := range a.attached {
		if !present[idx] {
			at.close()
			delete(a.attached, idx)
			a.forget(idx)
		}
	}
	return nil
}

func (a *Agent) attach(index int, name string) (attachment, error) {
	if a.mode != "tc" {
		in, err := link.AttachTCX(link.TCXOptions{Interface: index, Program: a.ingress, Attach: ebpf.AttachTCXIngress})
		if err == nil {
			out, err := link.AttachTCX(link.TCXOptions{Interface: index, Program: a.egress, Attach: ebpf.AttachTCXEgress})
			if err != nil {
				in.Close()
				return attachment{}, fmt.Errorf("TCX egress: %w", err)
			}
			a.mode = "tcx"
			return attachment{name: name, close: func() { in.Close(); out.Close() }}, nil
		}
		if !errors.Is(err, ebpf.ErrNotSupported) && a.mode == "tcx" {
			return attachment{}, fmt.Errorf("TCX ingress: %w", err)
		}
		a.mode = "tc" // kernel < 6.6: legacy clsact
	}
	return attachLegacy(index, name, a.ingress, a.egress)
}

// attachLegacy uses a clsact qdisc with direct-action filters (kernels without TCX).
// clsact sits next to the root qdisc, so tarvos-qos shaping is unaffected.
func attachLegacy(index int, name string, ingress, egress *ebpf.Program) (attachment, error) {
	qdisc := &netlink.GenericQdisc{
		QdiscAttrs: netlink.QdiscAttrs{LinkIndex: index, Handle: netlink.MakeHandle(0xffff, 0), Parent: netlink.HANDLE_CLSACT},
		QdiscType:  "clsact",
	}
	if err := netlink.QdiscReplace(qdisc); err != nil {
		return attachment{}, fmt.Errorf("clsact qdisc: %w", err)
	}
	var filters []*netlink.BpfFilter
	for _, hook := range []struct {
		parent uint32
		prog   *ebpf.Program
	}{{netlink.HANDLE_MIN_INGRESS, ingress}, {netlink.HANDLE_MIN_EGRESS, egress}} {
		f := &netlink.BpfFilter{
			FilterAttrs:  netlink.FilterAttrs{LinkIndex: index, Parent: hook.parent, Handle: 1, Protocol: unix.ETH_P_ALL, Priority: 1},
			Fd:           hook.prog.FD(),
			Name:         "tarvos",
			DirectAction: true,
		}
		if err := netlink.FilterReplace(f); err != nil {
			return attachment{}, fmt.Errorf("tc filter: %w", err)
		}
		filters = append(filters, f)
	}
	return attachment{name: name, close: func() {
		for _, f := range filters {
			_ = netlink.FilterDel(f)
		}
	}}, nil
}

// forget deletes the map entries of a removed interface (ifindexes are reused).
func (a *Agent) forget(index int) {
	var ik bpf.TarvosIfKey
	var cpk bpf.TarvosCpKey
	var dk bpf.TarvosDropKey
	var stale []any
	it := a.ifStats.Iterate()
	var iv []bpf.TarvosIfValue
	for it.Next(&ik, &iv) {
		if int(ik.Ifindex) == index {
			stale = append(stale, ik)
		}
	}
	for _, k := range stale {
		_ = a.ifStats.Delete(k)
	}
	stale = stale[:0]
	cit := a.cpStats.Iterate()
	var cv []bpf.TarvosCpValue
	for cit.Next(&cpk, &cv) {
		if int(cpk.Ifindex) == index {
			stale = append(stale, cpk)
		}
	}
	for _, k := range stale {
		_ = a.cpStats.Delete(k)
	}
	stale = stale[:0]
	dit := a.drops.Iterate()
	var dv []uint64
	for dit.Next(&dk, &dv) {
		if int(dk.Ifindex) == index {
			stale = append(stale, dk)
		}
	}
	for _, k := range stale {
		_ = a.drops.Delete(k)
	}
}

type linkInfo struct {
	name string
	kind string
	vni  uint32
}

// Snapshot reads every map (summing the per-CPU values).
func (a *Agent) Snapshot() (*Snapshot, error) {
	links, err := netlink.LinkList()
	if err != nil {
		return nil, fmt.Errorf("listing links: %w", err)
	}
	info := map[int]linkInfo{}
	for _, l := range links {
		li := linkInfo{name: l.Attrs().Name, kind: l.Type()}
		if vx, ok := l.(*netlink.Vxlan); ok {
			li.vni = uint32(vx.VxlanId)
		}
		info[l.Attrs().Index] = li
	}
	a.mu.Lock()
	mode := a.mode
	attached := map[int]bool{}
	for idx := range a.attached {
		attached[idx] = true
	}
	a.mu.Unlock()

	raw := Raw{Traffic: map[RawKey]RawTraffic{}, Control: map[RawControlKey]Counter{}, Drops: map[RawDropKey]uint64{}}
	var ik bpf.TarvosIfKey
	var iv []bpf.TarvosIfValue
	it := a.ifStats.Iterate()
	for it.Next(&ik, &iv) {
		var sum RawTraffic
		for _, v := range iv {
			sum.Packets += v.Packets
			sum.Bytes += v.Bytes
			for i := range sum.Sizes {
				sum.Sizes[i] += v.Size[i]
			}
		}
		raw.Traffic[RawKey{Ifindex: int(ik.Ifindex), Dir: ik.Dir}] = sum
	}
	if err := it.Err(); err != nil {
		return nil, fmt.Errorf("reading if_stats: %w", err)
	}
	var ck bpf.TarvosCpKey
	var cv []bpf.TarvosCpValue
	cit := a.cpStats.Iterate()
	for cit.Next(&ck, &cv) {
		var sum Counter
		for _, v := range cv {
			sum.Packets += v.Packets
			sum.Bytes += v.Bytes
		}
		raw.Control[RawControlKey{Ifindex: int(ck.Ifindex), Proto: ck.Proto, Dir: uint32(ck.Dir)}] = sum
	}
	if err := cit.Err(); err != nil {
		return nil, fmt.Errorf("reading cp_stats: %w", err)
	}
	var dk bpf.TarvosDropKey
	var dv []uint64
	dit := a.drops.Iterate()
	for dit.Next(&dk, &dv) {
		var sum uint64
		for _, v := range dv {
			sum += v
		}
		raw.Drops[RawDropKey{Ifindex: int(dk.Ifindex), Reason: dk.Reason}] = sum
	}
	if err := dit.Err(); err != nil {
		return nil, fmt.Errorf("reading drop_stats: %w", err)
	}
	snap := Build(raw, info, attached, a.reasons)
	snap.DropTracing = a.dropLink != nil
	snap.Attach = mode
	return snap, nil
}

// Raw map contents, summed over CPUs (input of Build, so it is testable without BPF).
type Raw struct {
	Traffic map[RawKey]RawTraffic
	Control map[RawControlKey]Counter
	Drops   map[RawDropKey]uint64
}

// RawKey identifies an interface and direction.
type RawKey struct {
	Ifindex int
	Dir     uint32
}

// RawTraffic is the if_stats value.
type RawTraffic struct {
	Packets, Bytes uint64
	Sizes          [8]uint64
}

// RawControlKey is the cp_stats key.
type RawControlKey struct {
	Ifindex int
	Proto   uint16
	Dir     uint32
}

// RawDropKey is the drop_stats key.
type RawDropKey struct {
	Ifindex int
	Reason  uint32
}

// Build turns raw counters into a Snapshot. Only instrumented, existing interfaces are
// reported (drops are reported for every existing interface: kfree_skb sees them all).
func Build(raw Raw, info map[int]linkInfo, attached map[int]bool, reasons map[uint32]string) *Snapshot {
	snap := &Snapshot{Time: time.Now().UTC(), SizeBuckets: SizeBuckets}
	byIndex := map[int]*Interface{}
	get := func(idx int) *Interface {
		if iface, ok := byIndex[idx]; ok {
			return iface
		}
		li, ok := info[idx]
		if !ok {
			return nil
		}
		iface := &Interface{Name: li.name, Index: idx, Kind: li.kind, VNI: li.vni, Instrumented: attached[idx],
			RX: Traffic{Sizes: make([]uint64, len(SizeBuckets)+1)},
			TX: Traffic{Sizes: make([]uint64, len(SizeBuckets)+1)}}
		byIndex[idx] = iface
		return iface
	}
	for idx := range attached {
		get(idx)
	}
	for key, value := range raw.Traffic {
		if !attached[key.Ifindex] {
			continue
		}
		iface := get(key.Ifindex)
		if iface == nil {
			continue
		}
		dir := &iface.RX
		if key.Dir == 1 {
			dir = &iface.TX
		}
		dir.Packets, dir.Bytes = value.Packets, value.Bytes
		copy(dir.Sizes, value.Sizes[:])
	}
	for key, value := range raw.Drops {
		iface := get(key.Ifindex)
		if iface == nil {
			continue
		}
		iface.Drops += value
		name, ok := reasons[key.Reason]
		if !ok {
			name = fmt.Sprintf("reason_%d", key.Reason)
		}
		if iface.DropReasons == nil {
			iface.DropReasons = map[string]uint64{}
		}
		iface.DropReasons[name] += value
	}
	for key, value := range raw.Control {
		li, ok := info[key.Ifindex]
		proto, known := Protocols[key.Proto]
		if !ok || !known || !attached[key.Ifindex] {
			continue
		}
		snap.ControlPlane = append(snap.ControlPlane, ControlPlane{
			Interface: li.name, Protocol: proto, Direction: Directions[key.Dir], Counter: value,
		})
	}
	for _, iface := range byIndex {
		snap.Interfaces = append(snap.Interfaces, *iface)
		if iface.VNI != 0 {
			snap.VNIs = append(snap.VNIs, VNI{
				VNI: iface.VNI, Interface: iface.Name, Drops: iface.Drops,
				RX: Counter{Packets: iface.RX.Packets, Bytes: iface.RX.Bytes},
				TX: Counter{Packets: iface.TX.Packets, Bytes: iface.TX.Bytes},
			})
		}
	}
	sort.Slice(snap.Interfaces, func(i, j int) bool { return snap.Interfaces[i].Name < snap.Interfaces[j].Name })
	sort.Slice(snap.VNIs, func(i, j int) bool { return snap.VNIs[i].VNI < snap.VNIs[j].VNI })
	sort.Slice(snap.ControlPlane, func(i, j int) bool {
		x, y := snap.ControlPlane[i], snap.ControlPlane[j]
		if x.Interface != y.Interface {
			return x.Interface < y.Interface
		}
		if x.Protocol != y.Protocol {
			return x.Protocol < y.Protocol
		}
		return x.Direction < y.Direction
	})
	if snap.Interfaces == nil {
		snap.Interfaces = []Interface{}
	}
	if snap.VNIs == nil {
		snap.VNIs = []VNI{}
	}
	if snap.ControlPlane == nil {
		snap.ControlPlane = []ControlPlane{}
	}
	return snap
}
