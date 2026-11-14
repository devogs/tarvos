module github.com/devogs/tarvos/telemetry

go 1.24

require (
	github.com/cilium/ebpf v0.17.3
	github.com/vishvananda/netlink v1.3.0
	golang.org/x/sys v0.30.0
)

require github.com/vishvananda/netns v0.0.4 // indirect

tool github.com/cilium/ebpf/cmd/bpf2go
