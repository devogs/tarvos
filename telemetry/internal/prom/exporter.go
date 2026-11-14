// Package prom exposes the node's metrics in the Prometheus text format (/metrics):
// the eBPF counters (one agent snapshot per scrape; the maps are never read here
// directly), the container's own resources, kernel interface gauges, and the routing
// and kernel-feature state also served over NETCONF and gNMI (cached, see state).
package prom

import (
	"errors"
	"net"
	"net/http"
	"strconv"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"

	"github.com/devogs/tarvos/telemetry/internal/agent"
	"github.com/devogs/tarvos/telemetry/internal/node"
	"github.com/devogs/tarvos/telemetry/internal/state"
)

const namespace = "tarvos"

var (
	ifPackets = prometheus.NewDesc(namespace+"_interface_packets_total",
		"Packets seen by the eBPF tc programs.", []string{"interface", "direction"}, nil)
	ifBytes = prometheus.NewDesc(namespace+"_interface_bytes_total",
		"Bytes seen by the eBPF tc programs.", []string{"interface", "direction"}, nil)
	ifSizes = prometheus.NewDesc(namespace+"_interface_packet_size_bytes",
		"Packet sizes (histogram).", []string{"interface", "direction"}, nil)
	ifDrops = prometheus.NewDesc(namespace+"_interface_drops_total",
		"Packets dropped by the kernel (kfree_skb), by drop reason.",
		[]string{"interface", "reason"}, nil)
	vniPackets = prometheus.NewDesc(namespace+"_vni_packets_total",
		"Packets of a VNI (its VXLAN device): rx decapsulated, tx to be encapsulated.",
		[]string{"vni", "interface", "direction"}, nil)
	vniBytes = prometheus.NewDesc(namespace+"_vni_bytes_total",
		"Bytes of a VNI (its VXLAN device).", []string{"vni", "interface", "direction"}, nil)
	vniDrops = prometheus.NewDesc(namespace+"_vni_drops_total",
		"Packets dropped on the VXLAN device of a VNI.", []string{"vni", "interface"}, nil)
	cpPackets = prometheus.NewDesc(namespace+"_control_plane_packets_total",
		"Control-plane protocol packets.", []string{"interface", "protocol", "direction"}, nil)
	cpBytes = prometheus.NewDesc(namespace+"_control_plane_bytes_total",
		"Control-plane protocol bytes.", []string{"interface", "protocol", "direction"}, nil)
	info = prometheus.NewDesc(namespace+"_telemetry_info",
		"Telemetry agent: version, attach mode (tcx or tc) and drop tracing.",
		[]string{"version", "attach", "drop_tracing"}, nil)
	up = prometheus.NewDesc(namespace+"_telemetry_up",
		"1 when the last snapshot of the agent succeeded.", nil, nil)
)

// Collector turns snapshots into metrics.
type Collector struct {
	Source     agent.Source
	Version    string
	FRRVersion string
	// FS reads the container's cgroup and /proc (zero value: the real root).
	FS node.FS
	// Node: container, process, eBPF, interface gauges (PROMETHEUS_KERNEL_METRICS).
	Node bool
	// State: routing and kernel-feature state (nil: off).
	State *state.Fetcher

	mu     sync.Mutex
	speeds map[int]speedEntry
}

// Describe implements prometheus.Collector.
func (c *Collector) Describe(ch chan<- *prometheus.Desc) {
	for _, d := range []*prometheus.Desc{ifPackets, ifBytes, ifSizes, ifDrops, vniPackets,
		vniBytes, vniDrops, cpPackets, cpBytes, info, up} {
		ch <- d
	}
	for _, d := range nodeDescs {
		ch <- d
	}
	for _, d := range stateDescs {
		ch <- d
	}
}

// Collect implements prometheus.Collector.
func (c *Collector) Collect(ch chan<- prometheus.Metric) {
	start := time.Now()
	defer func() { gauge(ch, scrapeDuration, time.Since(start).Seconds()) }()
	if c.Node {
		c.collectNode(ch)
		c.collectLinks(ch)
	}
	c.collectState(ch)
	snap, err := c.Source.Snapshot()
	if err != nil {
		ch <- prometheus.MustNewConstMetric(up, prometheus.GaugeValue, 0)
		return
	}
	gauge(ch, snapshotAge, max(0, time.Since(snap.Time).Seconds()))
	ch <- prometheus.MustNewConstMetric(up, prometheus.GaugeValue, 1)
	ch <- prometheus.MustNewConstMetric(info, prometheus.GaugeValue, 1,
		c.Version, snap.Attach, strconv.FormatBool(snap.DropTracing))
	counter := func(d *prometheus.Desc, v uint64, labels ...string) {
		ch <- prometheus.MustNewConstMetric(d, prometheus.CounterValue, float64(v), labels...)
	}
	for _, iface := range snap.Interfaces {
		if iface.Instrumented {
			for _, dir := range []struct {
				name string
				t    agent.Traffic
			}{{"rx", iface.RX}, {"tx", iface.TX}} {
				counter(ifPackets, dir.t.Packets, iface.Name, dir.name)
				counter(ifBytes, dir.t.Bytes, iface.Name, dir.name)
				ch <- histogram(dir.t, snap.SizeBuckets, iface.Name, dir.name)
			}
		}
		for reason, n := range iface.DropReasons {
			counter(ifDrops, n, iface.Name, reason)
		}
	}
	for _, v := range snap.VNIs {
		vni := strconv.FormatUint(uint64(v.VNI), 10)
		counter(vniPackets, v.RX.Packets, vni, v.Interface, "rx")
		counter(vniPackets, v.TX.Packets, vni, v.Interface, "tx")
		counter(vniBytes, v.RX.Bytes, vni, v.Interface, "rx")
		counter(vniBytes, v.TX.Bytes, vni, v.Interface, "tx")
		counter(vniDrops, v.Drops, vni, v.Interface)
	}
	for _, cp := range snap.ControlPlane {
		counter(cpPackets, cp.Packets, cp.Interface, cp.Protocol, cp.Direction)
		counter(cpBytes, cp.Bytes, cp.Interface, cp.Protocol, cp.Direction)
	}
}

// histogram converts per-bucket counts into a cumulative Prometheus histogram. The sum
// is the byte counter (exact: every packet's length is added to it).
func histogram(t agent.Traffic, bounds []uint64, labels ...string) prometheus.Metric {
	buckets := make(map[float64]uint64, len(bounds))
	var cumulative uint64
	for i, bound := range bounds {
		if i < len(t.Sizes) {
			cumulative += t.Sizes[i]
		}
		buckets[float64(bound)] = cumulative
	}
	return prometheus.MustNewConstHistogram(ifSizes, t.Packets, float64(t.Bytes), buckets, labels...)
}

// Handler serves /metrics for c (plus the agent process's own metrics).
func Handler(c *Collector) http.Handler {
	registry := prometheus.NewRegistry()
	registry.MustRegister(c)
	registry.MustRegister(collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}))
	mux := http.NewServeMux()
	// A series that cannot be built must not empty the whole scrape.
	mux.Handle("GET /metrics", promhttp.HandlerFor(registry,
		promhttp.HandlerOpts{ErrorHandling: promhttp.ContinueOnError}))
	mux.HandleFunc("GET /", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("Tarvos telemetry: /metrics\n"))
	})
	return mux
}

// Serve runs the exporter on listener until it is closed.
func Serve(listener net.Listener, c *Collector) error {
	server := &http.Server{Handler: Handler(c), ReadHeaderTimeout: 10 * time.Second}
	if err := server.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) &&
		!errors.Is(err, net.ErrClosed) {
		return err
	}
	return nil
}
