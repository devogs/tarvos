package agent

// Protocols maps the C enum proto (telemetry/bpf/tarvos.bpf.c) to label values.
var Protocols = map[uint16]string{
	1:  "bgp",
	2:  "bfd",
	3:  "ldp",
	4:  "ospf",
	5:  "isis",
	6:  "vrrp",
	7:  "pim",
	8:  "lldp",
	9:  "vxlan",
	10: "ssh",
	11: "netconf",
	12: "tacacs",
	13: "gnmi",
	14: "prometheus",
}

// Direction labels (C DIR_RX, DIR_TX).
var Directions = map[uint32]string{0: "rx", 1: "tx"}
