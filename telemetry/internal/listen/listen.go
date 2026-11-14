// Package listen opens the TCP listeners of the exporters, optionally bound to a VRF.
package listen

import (
	"context"
	"fmt"
	"log"
	"net"
	"strings"
	"syscall"

	"golang.org/x/sys/unix"
)

// VRF returns the VRF the exporters listen in: MGMT_VRF_NAME when the management VRF
// is enabled (the default), unless TELEMETRY_LISTEN_VRF overrides it ("none": every VRF,
// through the default one, like sshd and netopeer2 with tcp_l3mdev_accept).
// lookup is os.LookupEnv (injectable for tests).
func VRF(lookup func(string) (string, bool)) string {
	if v, ok := lookup("TELEMETRY_LISTEN_VRF"); ok && strings.TrimSpace(v) != "" {
		if v = strings.TrimSpace(v); strings.EqualFold(v, "none") {
			return ""
		}
		return v
	}
	enabled, _ := lookup("MGMT_VRF_ENABLED")
	switch strings.ToLower(strings.TrimSpace(enabled)) {
	case "", "yes", "true", "1":
		if name, _ := lookup("MGMT_VRF_NAME"); strings.TrimSpace(name) != "" {
			return strings.TrimSpace(name)
		}
		return "mgmt"
	}
	return ""
}

// TCP listens on port, bound to vrf (SO_BINDTODEVICE) when it exists. A missing VRF
// device (no VRF support in the host kernel) falls back to all addresses.
func TCP(port int, vrf string) (net.Listener, error) {
	if vrf != "" {
		if _, err := net.InterfaceByName(vrf); err != nil {
			log.Printf("WARNING: VRF %s not found, listening on port %d in the default VRF", vrf, port)
			vrf = ""
		}
	}
	config := net.ListenConfig{}
	if vrf != "" {
		config.Control = func(_, _ string, c syscall.RawConn) error {
			var serr error
			if err := c.Control(func(fd uintptr) {
				serr = unix.SetsockoptString(int(fd), unix.SOL_SOCKET, unix.SO_BINDTODEVICE, vrf)
			}); err != nil {
				return err
			}
			return serr
		}
	}
	l, err := config.Listen(context.Background(), "tcp", fmt.Sprintf(":%d", port))
	if err != nil {
		return nil, fmt.Errorf("listening on port %d (VRF %q): %w", port, vrf, err)
	}
	return l, nil
}
