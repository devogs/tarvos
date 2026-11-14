package agent

import (
	"encoding/json"
	"testing"
)

func TestBuildSnapshot(t *testing.T) {
	info := map[int]linkInfo{
		2: {name: "eth1", kind: "veth"},
		5: {name: "vx100", kind: "vxlan", vni: 100},
		7: {name: "RED", kind: "vrf"},
	}
	attached := map[int]bool{2: true, 5: true}
	raw := Raw{
		Traffic: map[RawKey]RawTraffic{
			{Ifindex: 2, Dir: 0}: {Packets: 10, Bytes: 1000, Sizes: [8]uint64{4, 6}},
			{Ifindex: 2, Dir: 1}: {Packets: 3, Bytes: 300},
			{Ifindex: 5, Dir: 0}: {Packets: 7, Bytes: 700},
			{Ifindex: 9, Dir: 0}: {Packets: 1, Bytes: 1}, // gone interface: ignored
			{Ifindex: 7, Dir: 0}: {Packets: 1, Bytes: 1}, // not instrumented: ignored
		},
		Control: map[RawControlKey]Counter{
			{Ifindex: 2, Proto: 1, Dir: 0}:  {Packets: 5, Bytes: 400},
			{Ifindex: 2, Proto: 99, Dir: 0}: {Packets: 1, Bytes: 1}, // unknown protocol
		},
		Drops: map[RawDropKey]uint64{
			{Ifindex: 7, Reason: 3}:  4, // drops on a VRF master are still reported
			{Ifindex: 2, Reason: 3}:  1,
			{Ifindex: 2, Reason: 77}: 2,
		},
	}
	snap := Build(raw, info, attached, map[uint32]string{3: "no_socket"})

	if len(snap.Interfaces) != 3 {
		t.Fatalf("interfaces: %+v", snap.Interfaces)
	}
	byName := map[string]Interface{}
	for _, i := range snap.Interfaces {
		byName[i.Name] = i
	}
	eth := byName["eth1"]
	if !eth.Instrumented || eth.RX.Packets != 10 || eth.TX.Bytes != 300 || eth.RX.Sizes[1] != 6 {
		t.Errorf("eth1: %+v", eth)
	}
	if len(eth.RX.Sizes) != len(SizeBuckets)+1 {
		t.Errorf("size buckets: %d", len(eth.RX.Sizes))
	}
	if eth.Drops != 3 || eth.DropReasons["no_socket"] != 1 || eth.DropReasons["reason_77"] != 2 {
		t.Errorf("eth1 drops: %+v", eth.DropReasons)
	}
	red := byName["RED"]
	if red.Instrumented || red.RX.Packets != 0 || red.Drops != 4 {
		t.Errorf("RED: %+v", red)
	}
	if len(snap.VNIs) != 1 || snap.VNIs[0].VNI != 100 || snap.VNIs[0].RX.Packets != 7 {
		t.Errorf("vnis: %+v", snap.VNIs)
	}
	if len(snap.ControlPlane) != 1 || snap.ControlPlane[0].Protocol != "bgp" ||
		snap.ControlPlane[0].Direction != "rx" || snap.ControlPlane[0].Packets != 5 {
		t.Errorf("control plane: %+v", snap.ControlPlane)
	}
}

func TestEmptySnapshotEncodesEmptyLists(t *testing.T) {
	out, err := json.Marshal(Build(Raw{}, nil, nil, nil))
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	_ = json.Unmarshal(out, &decoded)
	for _, key := range []string{"interfaces", "vnis", "control_plane"} {
		if list, ok := decoded[key].([]any); !ok || len(list) != 0 {
			t.Errorf("%s: %v", key, decoded[key])
		}
	}
}

func TestInstrumented(t *testing.T) {
	a := &Agent{cfg: Config{Exclude: []string{"lo", "vrrp*"}}}
	cases := map[[2]string]bool{
		{"eth1", "veth"}:       true,
		{"vx100", "vxlan"}:     true,
		{"lo", "device"}:       false,
		{"vrrp4-7", "macvlan"}: false,
		{"RED", "vrf"}:         false,
	}
	for c, want := range cases {
		if got := Instrumented(c[0], c[1], a.excluded); got != want {
			t.Errorf("%v: got %v", c, got)
		}
	}
}

func TestProtocolsMatchTheCEnum(t *testing.T) {
	// enum proto in tarvos.bpf.c starts at 1 and is contiguous.
	for i := uint16(1); i <= uint16(len(Protocols)); i++ {
		if Protocols[i] == "" {
			t.Errorf("protocol %d has no name", i)
		}
	}
}
