package prom

import (
	"io"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/devogs/tarvos/telemetry/internal/agent"
)

func drain(ch chan prometheus.Metric, done chan struct{}) {
	for range ch {
	}
	close(done)
}

func BenchmarkCollectNode(b *testing.B) {
	c := &Collector{Source: fakeSource{snap: &agent.Snapshot{Time: time.Now()}}, Node: true}
	for i := 0; i < b.N; i++ {
		ch, done := make(chan prometheus.Metric, 64), make(chan struct{})
		go drain(ch, done)
		c.collectNode(ch)
		close(ch)
		<-done
	}
}

func BenchmarkCollectLinks(b *testing.B) {
	c := &Collector{Source: fakeSource{snap: &agent.Snapshot{Time: time.Now()}}, Node: true}
	for i := 0; i < b.N; i++ {
		ch, done := make(chan prometheus.Metric, 64), make(chan struct{})
		go drain(ch, done)
		c.collectLinks(ch)
		close(ch)
		<-done
	}
}

func BenchmarkScrapeNode(b *testing.B) {
	h := Handler(&Collector{Source: fakeSource{snap: &agent.Snapshot{Time: time.Now()}}, Node: true})
	for i := 0; i < b.N; i++ {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest("GET", "/metrics", nil))
		_, _ = io.ReadAll(rec.Result().Body)
	}
}
