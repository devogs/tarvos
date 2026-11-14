// tarvos-telemetry: the Tarvos kernel telemetry agent.
//
//	tarvos-telemetry run       load the eBPF programs, instrument every interface and
//	                           serve the counters on the local socket
//	tarvos-telemetry snapshot  print the counters of the running agent (JSON)
//
// Configuration comes from the environment (see .env.example): TELEMETRY_EXCLUDE,
// TELEMETRY_SYNC_INTERVAL, TELEMETRY_SOCKET, TELEMETRY_LISTEN_VRF, PROMETHEUS_ENABLED,
// PROMETHEUS_PORT, GNMI_ENABLED, GNMI_PORT, GNMI_TLS, GNMI_TLS_CERT, GNMI_TLS_KEY.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"

	"github.com/devogs/tarvos/telemetry/internal/agent"
	"github.com/devogs/tarvos/telemetry/internal/api"
	"github.com/devogs/tarvos/telemetry/internal/gnmisrv"
	"github.com/devogs/tarvos/telemetry/internal/listen"
	"github.com/devogs/tarvos/telemetry/internal/prom"
)

var version = "dev"

func env(name, fallback string) string {
	if v, ok := os.LookupEnv(name); ok && strings.TrimSpace(v) != "" {
		return strings.TrimSpace(v)
	}
	return fallback
}

// splitList accepts "a,b c" and "[a, b]".
func splitList(raw string) []string {
	raw = strings.Trim(strings.TrimSpace(raw), "[]")
	var out []string
	for _, item := range strings.FieldsFunc(raw, func(r rune) bool { return r == ',' || r == ' ' }) {
		if item = strings.Trim(item, `"'`); item != "" {
			out = append(out, item)
		}
	}
	return out
}

func enabled(name string, fallback bool) bool {
	switch strings.ToLower(env(name, strconv.FormatBool(fallback))) {
	case "yes", "true", "1", "on":
		return true
	case "no", "false", "0", "off":
		return false
	}
	log.Printf("WARNING: %s must be yes or no; using %v", name, fallback)
	return fallback
}

func port(name string, fallback int) int {
	n, err := strconv.Atoi(env(name, strconv.Itoa(fallback)))
	if err != nil || n < 1 || n > 65535 {
		log.Printf("WARNING: %s must be a TCP port; using %d", name, fallback)
		return fallback
	}
	return n
}

func seconds(name string, fallback int) time.Duration {
	n, err := strconv.Atoi(env(name, strconv.Itoa(fallback)))
	if err != nil || n < 1 || n > 3600 {
		log.Printf("WARNING: %s must be 1-3600 seconds; using %d", name, fallback)
		n = fallback
	}
	return time.Duration(n) * time.Second
}

func run() error {
	exclude := splitList(env("TELEMETRY_EXCLUDE", "lo"))
	a, err := agent.New(agent.Config{Exclude: exclude})
	if err != nil {
		return err
	}
	defer a.Close()
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	if err := a.Sync(); err != nil {
		return err
	}
	interval := seconds("TELEMETRY_SYNC_INTERVAL", 5)
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if err := a.Sync(); err != nil {
					log.Printf("ERROR: %v", err)
				}
			}
		}
	}()
	vrf := listen.VRF(os.LookupEnv)
	if enabled("PROMETHEUS_ENABLED", true) {
		p := port("PROMETHEUS_PORT", 9273)
		l, err := listen.TCP(p, vrf)
		if err != nil {
			return err
		}
		go func() { <-ctx.Done(); l.Close() }()
		go func() {
			if err := prom.Serve(l, a, version); err != nil {
				log.Printf("ERROR: Prometheus exporter: %v", err)
			}
		}()
		log.Printf("Prometheus exporter on port %d (VRF %q)", p, vrf)
	}
	if enabled("GNMI_ENABLED", true) {
		if err := startGNMI(ctx, a, vrf); err != nil {
			return err
		}
	}
	socket := env("TELEMETRY_SOCKET", api.DefaultSocket)
	log.Printf("serving counters on %s (version %s)", socket, version)
	return api.Serve(ctx, socket, a)
}

// startGNMI serves gNMI on GNMI_PORT (57400), authenticated through PAM (service gnmi:
// local admin and TACACS+ users, as for NETCONF).
func startGNMI(ctx context.Context, a agent.Source, vrf string) error {
	access := strings.ToLower(env("TACACS_NETCONF_ACCESS", "ro"))
	srv := &gnmisrv.Server{
		Data:  &gnmisrv.Sources{Agent: a, Version: version, Sysrepocfg: env("SYSREPOCFG", "sysrepocfg")},
		Index: gnmisrv.LoadIndex(env("YANG_INDEX", "/run/tarvos/yang-index.json")),
		Auth:  gnmisrv.PAM{Service: "gnmi"},
		// gNMI is read-only: TACACS+ users need at least read access.
		Allowed: func(user string) bool { return user == "admin" || access != "none" },
	}
	opts := srv.Interceptors()
	security := "TLS"
	if enabled("GNMI_TLS", true) {
		cert, fp, generated, err := gnmisrv.LoadOrCreateCert(env("GNMI_TLS_CERT", "/etc/tarvos/gnmi/server.crt"),
			env("GNMI_TLS_KEY", "/etc/tarvos/gnmi/server.key"), "/run/tarvos/gnmi")
		if err != nil {
			return err
		}
		if generated {
			security = "TLS, self-signed /run/tarvos/gnmi/server.crt"
		}
		log.Printf("gNMI certificate SHA-256 %s", fp)
		opts = append(opts, grpc.Creds(credentials.NewServerTLSFromCert(&cert)))
	} else {
		security = "plaintext (GNMI_TLS=no: passwords are sent in clear)"
		log.Printf("WARNING: gNMI without TLS")
	}
	g := grpc.NewServer(opts...)
	srv.Register(g)
	p := port("GNMI_PORT", 57400)
	l, err := listen.TCP(p, vrf)
	if err != nil {
		return err
	}
	go func() { <-ctx.Done(); g.Stop() }()
	go func() {
		if err := g.Serve(l); err != nil {
			log.Printf("ERROR: gNMI server: %v", err)
		}
	}()
	log.Printf("gNMI server on port %d (VRF %q, %s, %d YANG modules)", p, vrf, security, len(srv.Index.Modules))
	return nil
}

func snapshot() error {
	snap, err := api.Fetch(env("TELEMETRY_SOCKET", api.DefaultSocket))
	if err != nil {
		return err
	}
	out := json.NewEncoder(os.Stdout)
	out.SetIndent("", "  ")
	return out.Encode(snap)
}

func main() {
	log.SetFlags(0)
	log.SetPrefix("[telemetry] ")
	command := "run"
	if len(os.Args) > 1 {
		command = os.Args[1]
	}
	var err error
	switch command {
	case "run":
		err = run()
	case "snapshot":
		err = snapshot()
	case "version":
		fmt.Println(version)
	default:
		err = fmt.Errorf("usage: tarvos-telemetry run|snapshot|version")
	}
	if err != nil {
		log.Printf("FATAL: %v", err)
		os.Exit(1)
	}
}
