package gnmisrv

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/vishvananda/netlink"

	"github.com/devogs/tarvos/telemetry/internal/agent"
	"github.com/devogs/tarvos/telemetry/internal/node"
)

const (
	ocInterfaces = "openconfig-interfaces:interfaces"
	telemetry    = "tarvos-telemetry:telemetry"
)

// LocalKeys are the list keys of the trees built here (see telemetry/yang).
var LocalKeys = Keys{
	"/" + ocInterfaces + "/interface":             {"name"},
	"/" + telemetry + "/interface":                {"name"},
	"/" + telemetry + "/interface/rx/size-bucket": {"le"},
	"/" + telemetry + "/interface/tx/size-bucket": {"le"},
	"/" + telemetry + "/interface/drop-reason":    {"reason"},
	"/" + telemetry + "/vni":                      {"id"},
	"/" + telemetry + "/control-plane":            {"interface", "protocol", "direction"},
	"/" + telemetry + "/container/process":        {"name"},
	"/" + telemetry + "/ebpf/map":                 {"name"},
	"/" + telemetry + "/ebpf/program":             {"name"},
}

// Index is the YANG index written by scripts/yang_index.py.
type Index struct {
	Tops    map[string][]string `json:"tops"`
	Keys    Keys                `json:"keys"`
	Modules map[string]string   `json:"modules"`
}

// LoadIndex reads the YANG index; a missing file gives an empty index (Sysrepo data
// is then only served as JSON subtrees).
func LoadIndex(path string) Index {
	idx := Index{Tops: map[string][]string{}, Keys: Keys{}, Modules: map[string]string{}}
	data, err := os.ReadFile(path)
	if err == nil {
		_ = json.Unmarshal(data, &idx)
	}
	for k, v := range LocalKeys {
		idx.Keys[k] = v
	}
	return idx
}

// Root is a resolved top-level node: "module:name".
type Root string

// Resolve finds the root of a path: origin "openconfig" (or none) with "interfaces" is
// OpenConfig; "module:name" names a module; an origin that is a module name is used
// as the prefix; an unprefixed name is looked up in the index when unambiguous.
func (idx Index) Resolve(origin string, first string) (Root, error) {
	if i := strings.IndexByte(first, ':'); i >= 0 {
		return Root(first), nil
	}
	switch {
	case first == "interfaces" && (origin == "" || origin == "openconfig" || origin == "openconfig-interfaces"):
		return ocInterfaces, nil
	case first == "telemetry" && (origin == "" || origin == "tarvos-telemetry"):
		return telemetry, nil
	case origin != "" && origin != "openconfig":
		return Root(origin + ":" + first), nil
	}
	modules := idx.Tops[first]
	if len(modules) == 1 {
		return Root(modules[0] + ":" + first), nil
	}
	if len(modules) > 1 {
		return "", fmt.Errorf("%q is defined by several modules (%s): prefix it, e.g. %s:%s",
			first, strings.Join(modules, ", "), modules[0], first)
	}
	return "", fmt.Errorf("unknown path /%s", first)
}

// Data fetches the tree of a root.
type Data interface {
	Fetch(ctx context.Context, root Root) (any, error)
}

// Sources is the production Data: the agent for the local trees, Sysrepo for the rest.
type Sources struct {
	Agent   agent.Source
	Version string
	// Sysrepocfg is the sysrepocfg binary.
	Sysrepocfg string
	// FS reads the container's cgroup and /proc (zero value: the real root).
	FS node.FS
}

// Fetch implements Data.
func (s *Sources) Fetch(ctx context.Context, root Root) (any, error) {
	switch root {
	case ocInterfaces:
		snap, err := s.Agent.Snapshot()
		if err != nil {
			return nil, err
		}
		return OpenConfigInterfaces(snap, linkStates()), nil
	case telemetry:
		snap, err := s.Agent.Snapshot()
		if err != nil {
			return nil, err
		}
		out := Telemetry(snap, s.Version).(map[string]any)
		if c := Container(s.FS); c != nil {
			out["container"] = c
		}
		if r, ok := s.Agent.(agent.BPFReporter); ok {
			out["ebpf"] = EBPF(r.BPF())
		}
		return out, nil
	}
	return s.sysrepo(ctx, string(root))
}

// sysrepo reads one top-level node of the operational datastore (configuration and
// state) as RFC 7951 JSON.
func (s *Sources) sysrepo(ctx context.Context, root string) (any, error) {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, s.Sysrepocfg, "--export", "--datastore", "operational",
		"--format", "json", "--xpath", "/"+root)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("reading /%s: %v %s", root, err, strings.TrimSpace(stderr.String()))
	}
	decoder := json.NewDecoder(bytes.NewReader(out))
	decoder.UseNumber()
	var doc map[string]any
	if len(bytes.TrimSpace(out)) == 0 {
		return nil, nil
	}
	if err := decoder.Decode(&doc); err != nil {
		return nil, fmt.Errorf("decoding /%s: %w", root, err)
	}
	return doc[root], nil
}

type linkState struct {
	index       int
	mtu         int
	adminUp     bool
	operStatus  string
	description string
}

func linkStates() map[string]linkState {
	out := map[string]linkState{}
	links, err := netlink.LinkList()
	if err != nil {
		return out
	}
	for _, l := range links {
		a := l.Attrs()
		out[a.Name] = linkState{
			index: a.Index, mtu: a.MTU, adminUp: a.Flags&1 != 0, // IFF_UP
			operStatus: ocOperStatus(a.OperState, a.RawFlags), description: a.Alias,
		}
	}
	return out
}

// ocOperStatus maps the kernel operational state to openconfig-interfaces oper-status.
func ocOperStatus(state netlink.LinkOperState, flags uint32) string {
	const iffLowerUp = 1 << 16
	switch state {
	case netlink.OperUp:
		return "UP"
	case netlink.OperDown:
		return "DOWN"
	case netlink.OperLowerLayerDown:
		return "LOWER_LAYER_DOWN"
	case netlink.OperDormant:
		return "DORMANT"
	case netlink.OperNotPresent:
		return "NOT_PRESENT"
	case netlink.OperTesting:
		return "TESTING"
	}
	if flags&1 != 0 && flags&iffLowerUp != 0 { // UNKNOWN (dummy, vxlan, ...) but running
		return "UP"
	}
	return "UNKNOWN"
}

// OpenConfigInterfaces builds /interfaces (openconfig-interfaces): state of every
// link, with the eBPF counters of the instrumented ones.
func OpenConfigInterfaces(snap *agent.Snapshot, links map[string]linkState) any {
	counters := map[string]agent.Interface{}
	for _, i := range snap.Interfaces {
		counters[i.Name] = i
	}
	names := make([]string, 0, len(links))
	for name := range links {
		names = append(names, name)
	}
	sort.Strings(names)
	var list []any
	for _, name := range names {
		l := links[name]
		admin := "DOWN"
		if l.adminUp {
			admin = "UP"
		}
		state := map[string]any{
			"name": name, "ifindex": uint32(l.index), "mtu": uint32(l.mtu),
			"admin-status": admin, "oper-status": l.operStatus,
		}
		if l.description != "" {
			state["description"] = l.description
		}
		if c, ok := counters[name]; ok && c.Instrumented {
			state["counters"] = map[string]any{
				"in-pkts": c.RX.Packets, "in-octets": c.RX.Bytes,
				"out-pkts": c.TX.Packets, "out-octets": c.TX.Bytes,
			}
		}
		list = append(list, map[string]any{"name": name, "config": map[string]any{"name": name}, "state": state})
	}
	return map[string]any{"interface": list}
}

func bucketLabel(i int, bounds []uint64) string {
	if i < len(bounds) {
		return strconv.FormatUint(bounds[i], 10)
	}
	return "+Inf"
}

func traffic(t agent.Traffic, bounds []uint64) map[string]any {
	var buckets []any
	for i, n := range t.Sizes {
		buckets = append(buckets, map[string]any{"le": bucketLabel(i, bounds), "packets": n})
	}
	return map[string]any{"packets": t.Packets, "bytes": t.Bytes, "size-bucket": buckets}
}

// Telemetry builds /tarvos-telemetry:telemetry from a snapshot.
func Telemetry(snap *agent.Snapshot, version string) any {
	var interfaces, vnis, cps []any
	for _, i := range snap.Interfaces {
		entry := map[string]any{
			"name": i.Name, "ifindex": uint32(i.Index), "instrumented": i.Instrumented, "drops": i.Drops,
		}
		if i.Kind != "" {
			entry["kind"] = i.Kind
		}
		if i.Instrumented {
			entry["rx"] = traffic(i.RX, snap.SizeBuckets)
			entry["tx"] = traffic(i.TX, snap.SizeBuckets)
		}
		var reasons []any
		names := make([]string, 0, len(i.DropReasons))
		for r := range i.DropReasons {
			names = append(names, r)
		}
		sort.Strings(names)
		for _, r := range names {
			reasons = append(reasons, map[string]any{"reason": r, "packets": i.DropReasons[r]})
		}
		if reasons != nil {
			entry["drop-reason"] = reasons
		}
		interfaces = append(interfaces, entry)
	}
	for _, v := range snap.VNIs {
		vnis = append(vnis, map[string]any{
			"id": v.VNI, "interface": v.Interface, "drops": v.Drops,
			"rx": map[string]any{"packets": v.RX.Packets, "bytes": v.RX.Bytes},
			"tx": map[string]any{"packets": v.TX.Packets, "bytes": v.TX.Bytes},
		})
	}
	for _, c := range snap.ControlPlane {
		cps = append(cps, map[string]any{
			"interface": c.Interface, "protocol": c.Protocol, "direction": c.Direction,
			"packets": c.Packets, "bytes": c.Bytes,
		})
	}
	out := map[string]any{
		"agent": map[string]any{"version": version, "attach": snap.Attach, "drop-tracing": snap.DropTracing},
	}
	for name, list := range map[string][]any{"interface": interfaces, "vni": vnis, "control-plane": cps} {
		if list != nil {
			out[name] = list
		}
	}
	return out
}

// Container builds /telemetry/container: the container's cgroup and its daemons.
func Container(fs node.FS) map[string]any {
	c, err := fs.Container()
	if err != nil {
		return nil
	}
	out := map[string]any{
		"cgroup-version": uint32(c.CgroupVersion), "cpu-seconds": c.CPUSeconds, "pids": c.PIDs,
	}
	for k, v := range c.Memory {
		out["memory-"+k] = v
	}
	if c.MemoryLimit > 0 {
		out["memory-limit"] = c.MemoryLimit
	}
	if up, err := fs.Uptime(); err == nil {
		out["uptime"] = up
	}
	if procs, err := fs.Processes(); err == nil {
		var list []any
		for _, p := range procs {
			list = append(list, map[string]any{
				"name": p.Name, "count": uint32(p.Count), "resident-memory": p.RSSBytes,
				"cpu-seconds": p.CPUSeconds,
			})
		}
		if list != nil {
			out["process"] = list
		}
	}
	return out
}

// EBPF builds /telemetry/ebpf from the agent's eBPF statistics.
func EBPF(stats agent.BPFStats) map[string]any {
	names := make([]string, 0, len(stats.Maps))
	for name := range stats.Maps {
		names = append(names, name)
	}
	sort.Strings(names)
	var maps, progs []any
	for _, name := range names {
		maps = append(maps, map[string]any{"name": name, "memory": stats.Maps[name]})
	}
	for _, p := range stats.Programs {
		entry := map[string]any{"name": p.Name, "type": p.Type}
		if stats.RunStats {
			entry["run-time"], entry["run-count"] = p.RunTimeNS, p.RunCount
		}
		progs = append(progs, entry)
	}
	out := map[string]any{"attached-interfaces": uint32(stats.AttachedInterfaces), "run-stats": stats.RunStats}
	if maps != nil {
		out["map"] = maps
	}
	if progs != nil {
		out["program"] = progs
	}
	return out
}
