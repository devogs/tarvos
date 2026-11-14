// Package agent owns the Tarvos eBPF programs and maps and is the single source of
// kernel counters. Consumers (the local API, the Prometheus exporter, the gNMI server)
// only ever see Snapshot values, never the maps.
package agent

import "time"

// SizeBuckets are the upper bounds (bytes, inclusive) of the packet-size histogram;
// the last bucket counts larger packets. Keep in sync with size_bucket() in the C code.
var SizeBuckets = []uint64{64, 128, 256, 512, 1024, 1518, 9216}

// Traffic of one interface in one direction.
type Traffic struct {
	Packets uint64 `json:"packets"`
	Bytes   uint64 `json:"bytes"`
	// Sizes[i] counts packets of at most SizeBuckets[i] bytes (and more than the
	// previous bound); the extra last element counts the larger ones.
	Sizes []uint64 `json:"sizes"`
}

// Interface counters.
type Interface struct {
	Name  string `json:"name"`
	Index int    `json:"ifindex"`
	Kind  string `json:"kind,omitempty"` // netlink kind: vxlan, bridge, veth, ...
	VNI   uint32 `json:"vni,omitempty"`  // VXLAN devices
	// Instrumented: traffic counters are collected (false for excluded interfaces and
	// VRF masters, which only appear for their drops).
	Instrumented bool    `json:"instrumented"`
	RX           Traffic `json:"rx"`
	TX           Traffic `json:"tx"`
	Drops        uint64  `json:"drops"`
	// Drop reasons (kernel enum skb_drop_reason, lower case without prefix).
	DropReasons map[string]uint64 `json:"drop_reasons,omitempty"`
}

// VNI counters: traffic of the VXLAN device of the VNI (rx: decapsulated from remote
// VTEPs, tx: to be encapsulated towards them).
type VNI struct {
	VNI       uint32  `json:"vni"`
	Interface string  `json:"interface"`
	RX        Counter `json:"rx"`
	TX        Counter `json:"tx"`
	Drops     uint64  `json:"drops"`
}

// Counter is a packet/byte pair.
type Counter struct {
	Packets uint64 `json:"packets"`
	Bytes   uint64 `json:"bytes"`
}

// ControlPlane counts the packets of one control-plane protocol on one interface.
type ControlPlane struct {
	Interface string `json:"interface"`
	Protocol  string `json:"protocol"`
	Direction string `json:"direction"` // rx | tx
	Counter
}

// Snapshot is a consistent-enough read of every counter (maps are read one after the
// other; each counter is monotonic).
type Snapshot struct {
	Time         time.Time      `json:"time"`
	SizeBuckets  []uint64       `json:"size_buckets"`
	DropTracing  bool           `json:"drop_tracing"` // kfree_skb program attached
	Attach       string         `json:"attach"`       // tcx | tc (legacy clsact)
	Interfaces   []Interface    `json:"interfaces"`
	VNIs         []VNI          `json:"vnis"`
	ControlPlane []ControlPlane `json:"control_plane"`
}

// Source is what the exporters depend on.
type Source interface {
	Snapshot() (*Snapshot, error)
}
