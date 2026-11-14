// Package api is the local interface of the telemetry agent: an HTTP/JSON endpoint on a
// Unix socket (root only), for consumers outside the agent process (tests, scripts,
// future backends). In-process exporters use agent.Source directly; neither touches
// the eBPF maps.
package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"github.com/devogs/tarvos/telemetry/internal/agent"
)

// DefaultSocket is where the agent listens.
const DefaultSocket = "/run/tarvos/telemetry.sock"

// Handler serves GET /v1/snapshot.
func Handler(src agent.Source) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/snapshot", func(w http.ResponseWriter, _ *http.Request) {
		snap, err := src.Snapshot()
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(snap)
	})
	return mux
}

// Serve listens on the Unix socket until ctx is done.
func Serve(ctx context.Context, socket string, src agent.Source) error {
	if err := os.MkdirAll(filepath.Dir(socket), 0o750); err != nil {
		return err
	}
	_ = os.Remove(socket)
	listener, err := net.Listen("unix", socket)
	if err != nil {
		return err
	}
	if err := os.Chmod(socket, 0o600); err != nil {
		listener.Close()
		return err
	}
	server := &http.Server{Handler: Handler(src), ReadHeaderTimeout: 5 * time.Second}
	go func() {
		<-ctx.Done()
		_ = server.Close()
	}()
	if err := server.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}

// Fetch reads a snapshot from a running agent.
func Fetch(socket string) (*agent.Snapshot, error) {
	client := &http.Client{
		Timeout: 10 * time.Second,
		Transport: &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", socket)
		}},
	}
	resp, err := client.Get("http://agent/v1/snapshot")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("agent: %s", resp.Status)
	}
	var snap agent.Snapshot
	if err := json.NewDecoder(resp.Body).Decode(&snap); err != nil {
		return nil, err
	}
	return &snap, nil
}
