package gnmisrv

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net"
	"strings"
	"sync"
	"testing"
	"time"

	gpb "github.com/openconfig/gnmi/proto/gnmi"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	"github.com/devogs/tarvos/telemetry/internal/agent"
)

// sysrepo-style JSON (as sysrepocfg prints it), decoded with UseNumber.
const bgpJSON = `{"state": {"instance": [
  {"vrf": "default", "asn": 65000, "neighbor": [
    {"address": "172.30.0.12", "session-state": "established", "uptime": "75",
     "address-family": [{"name": "l2vpn-evpn", "prefixes-received": 6}]},
    {"address": "192.0.2.9", "session-state": "active"}]},
  {"vrf": "RED", "asn": 65000, "l3vni": 5000}],
 "evpn": {"route-count": [{"route-type": 2, "count": 7}, {"route-type": 5, "count": 4}]}}}`

func decode(t *testing.T, s string) any {
	t.Helper()
	d := json.NewDecoder(bytes.NewReader([]byte(s)))
	d.UseNumber()
	var v any
	if err := d.Decode(&v); err != nil {
		t.Fatal(err)
	}
	return v
}

type fakeData struct {
	mu    sync.Mutex
	trees map[Root]any
}

func (f *fakeData) Fetch(_ context.Context, root Root) (any, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	v, ok := f.trees[root]
	if !ok {
		return nil, errors.New("no data")
	}
	return v, nil
}

func (f *fakeData) set(root Root, v any) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.trees[root] = v
}

type fakeAuth struct{ calls int }

func (a *fakeAuth) Authenticate(user, password, _ string) error {
	a.calls++
	if (user == "admin" && password == "admin") || (user == "alice" && password == "alicepw") {
		return nil
	}
	return errors.New("bad password")
}

var index = Index{
	Tops: map[string][]string{"bgp": {"tarvos-bgp"}, "lib": {"frr-interface", "frr-vrf"}},
	Keys: Keys{
		"/tarvos-bgp:bgp/state/instance":                         {"vrf"},
		"/tarvos-bgp:bgp/state/instance/neighbor":                {"address"},
		"/tarvos-bgp:bgp/state/instance/neighbor/address-family": {"name"},
		"/tarvos-bgp:bgp/state/evpn/route-count":                 {"route-type"},
	},
	Modules: map[string]string{"tarvos-bgp": "2026-11-14"},
}

func TestResolve(t *testing.T) {
	for _, c := range []struct{ origin, first, want, err string }{
		{"", "interfaces", ocInterfaces, ""},
		{"openconfig", "interfaces", ocInterfaces, ""},
		{"", "telemetry", telemetry, ""},
		{"", "bgp", "tarvos-bgp:bgp", ""},
		{"", "tarvos-bgp:bgp", "tarvos-bgp:bgp", ""},
		{"tarvos-bgp", "bgp", "tarvos-bgp:bgp", ""},
		{"", "lib", "", "several modules"},
		{"", "nope", "", "unknown"},
	} {
		got, err := index.Resolve(c.origin, c.first)
		if c.err != "" {
			if err == nil || !strings.Contains(err.Error(), c.err) {
				t.Errorf("%v: err %v", c, err)
			}
			continue
		}
		if err != nil || string(got) != c.want {
			t.Errorf("%v: got %q, %v", c, got, err)
		}
	}
}

func TestSelectAndFlatten(t *testing.T) {
	root := match{elems: []*gpb.PathElem{{Name: "bgp"}}, schema: "/tarvos-bgp:bgp", value: decode(t, bgpJSON)}
	path := []*gpb.PathElem{{Name: "state"}, {Name: "instance", Key: map[string]string{"vrf": "default"}},
		{Name: "neighbor", Key: map[string]string{"address": "*"}}, {Name: "session-state"}}
	got := Select(root, path, index.Keys)
	if len(got) != 2 {
		t.Fatalf("matches: %d", len(got))
	}
	leaves := Flatten(root, index.Keys)
	var paths []string
	for _, l := range leaves {
		paths = append(paths, PathString(l.Elems))
	}
	all := strings.Join(paths, "\n")
	for _, want := range []string{
		"/bgp/state/instance[vrf=default]/neighbor[address=172.30.0.12]/session-state",
		"/bgp/state/instance[vrf=default]/neighbor[address=172.30.0.12]/address-family[name=l2vpn-evpn]/prefixes-received",
		"/bgp/state/evpn/route-count[route-type=2]/count",
		"/bgp/state/instance[vrf=RED]/l3vni",
	} {
		if !strings.Contains(all, want) {
			t.Errorf("missing %s in\n%s", want, all)
		}
	}
}

func TestTypedValues(t *testing.T) {
	for v, want := range map[any]string{
		json.Number("65000"): "uint_val:65000",
		json.Number("-3"):    "int_val:-3",
		"established":        `string_val:"established"`,
		true:                 "bool_val:true",
		uint64(7):            "uint_val:7",
	} {
		tv, err := TypedValue(v)
		if err != nil || !strings.Contains(strings.ReplaceAll(tv.String(), " ", ""), strings.ReplaceAll(want, " ", "")) {
			t.Errorf("%v: %v %v", v, tv, err)
		}
	}
}

func TestLocalTrees(t *testing.T) {
	snap := &agent.Snapshot{
		SizeBuckets: agent.SizeBuckets, Attach: "tcx",
		Interfaces: []agent.Interface{{Name: "eth1", Index: 3, Instrumented: true,
			RX: agent.Traffic{Packets: 5, Bytes: 500, Sizes: []uint64{5, 0, 0, 0, 0, 0, 0, 0}},
			TX: agent.Traffic{Packets: 2, Bytes: 120, Sizes: make([]uint64, 8)}, Drops: 1,
			DropReasons: map[string]uint64{"no_socket": 1}}},
		VNIs: []agent.VNI{{VNI: 100, Interface: "vx100"}},
	}
	oc := OpenConfigInterfaces(snap, map[string]linkState{"eth1": {index: 3, mtu: 1500, adminUp: true, operStatus: "UP"}})
	m := match{elems: []*gpb.PathElem{{Name: "interfaces"}}, schema: "/" + ocInterfaces, value: oc}
	sel := Select(m, []*gpb.PathElem{{Name: "interface", Key: map[string]string{"name": "eth1"}},
		{Name: "state"}, {Name: "counters"}, {Name: "in-pkts"}}, LocalKeys)
	if len(sel) != 1 || sel[0].value != uint64(5) {
		t.Fatalf("in-pkts: %+v", sel)
	}
	tel := match{elems: []*gpb.PathElem{{Name: "telemetry"}}, schema: "/" + telemetry, value: Telemetry(snap, "2.1.0")}
	var paths []string
	for _, l := range Flatten(tel, LocalKeys) {
		paths = append(paths, PathString(l.Elems))
	}
	all := strings.Join(paths, "\n")
	for _, want := range []string{
		"/telemetry/interface[name=eth1]/rx/size-bucket[le=64]/packets",
		"/telemetry/interface[name=eth1]/rx/size-bucket[le=+Inf]/packets",
		"/telemetry/interface[name=eth1]/drop-reason[reason=no_socket]/packets",
		"/telemetry/vni[id=100]/rx/packets",
		"/telemetry/agent/attach",
	} {
		if !strings.Contains(all, want) {
			t.Errorf("missing %s in\n%s", want, all)
		}
	}
}

func dial(t *testing.T, data Data) (gpb.GNMIClient, *fakeAuth) {
	t.Helper()
	auth := &fakeAuth{}
	srv := &Server{Data: data, Index: index, Auth: auth, Allowed: func(u string) bool { return u != "mallory" }}
	lis := bufconn.Listen(1 << 20)
	g := grpc.NewServer(srv.Interceptors()...)
	srv.Register(g)
	go func() { _ = g.Serve(lis) }()
	t.Cleanup(g.Stop)
	conn, err := grpc.NewClient("passthrough:///bufnet",
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return lis.Dial() }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	return gpb.NewGNMIClient(conn), auth
}

func login(user, password string) context.Context {
	return metadata.AppendToOutgoingContext(context.Background(), "username", user, "password", password)
}

func p(elems ...*gpb.PathElem) *gpb.Path { return &gpb.Path{Elem: elems} }
func e(name string, kv ...string) *gpb.PathElem {
	el := &gpb.PathElem{Name: name}
	if len(kv) > 0 {
		el.Key = map[string]string{}
		for i := 0; i+1 < len(kv); i += 2 {
			el.Key[kv[i]] = kv[i+1]
		}
	}
	return el
}

func TestAuthentication(t *testing.T) {
	client, auth := dial(t, &fakeData{trees: map[Root]any{}})
	if _, err := client.Capabilities(context.Background(), &gpb.CapabilityRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Errorf("no credentials: %v", err)
	}
	if _, err := client.Capabilities(login("admin", "bad"), &gpb.CapabilityRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Errorf("bad password: %v", err)
	}
	caps, err := client.Capabilities(login("alice", "alicepw"), &gpb.CapabilityRequest{})
	if err != nil || caps.GNMIVersion != GNMIVersion || len(caps.SupportedModels) < 3 {
		t.Fatalf("capabilities: %v %v", caps, err)
	}
	calls := auth.calls
	_, _ = client.Capabilities(login("alice", "alicepw"), &gpb.CapabilityRequest{})
	if auth.calls != calls {
		t.Error("successful authentication not cached")
	}
	if _, err := client.Set(login("admin", "admin"), &gpb.SetRequest{}); status.Code(err) != codes.Unimplemented {
		t.Errorf("set: %v", err)
	}
}

func TestGet(t *testing.T) {
	client, _ := dial(t, &fakeData{trees: map[Root]any{"tarvos-bgp:bgp": decode(t, bgpJSON)}})
	ctx := login("admin", "admin")
	path := p(e("bgp"), e("state"), e("instance", "vrf", "default"), e("neighbor", "address", "172.30.0.12"), e("session-state"))
	resp, err := client.Get(ctx, &gpb.GetRequest{Path: []*gpb.Path{path}, Encoding: gpb.Encoding_PROTO})
	if err != nil {
		t.Fatal(err)
	}
	u := resp.Notification[0].Update
	if len(u) != 1 || u[0].Val.GetStringVal() != "established" {
		t.Fatalf("proto get: %v", u)
	}
	resp, err = client.Get(ctx, &gpb.GetRequest{Path: []*gpb.Path{p(e("bgp"), e("state"), e("evpn"))}, Encoding: gpb.Encoding_JSON_IETF})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(resp.Notification[0].Update[0].Val.GetJsonIetfVal()), `"route-count"`) {
		t.Errorf("json get: %v", resp)
	}
	if _, err := client.Get(ctx, &gpb.GetRequest{Path: []*gpb.Path{p(e("nope"))}}); status.Code(err) != codes.NotFound {
		t.Errorf("unknown path: %v", err)
	}
}

func TestSubscribeOnceAndOnChange(t *testing.T) {
	data := &fakeData{trees: map[Root]any{"tarvos-bgp:bgp": decode(t, bgpJSON)}}
	client, _ := dial(t, data)
	path := p(e("bgp"), e("state"), e("instance", "vrf", "default"), e("neighbor", "address", "192.0.2.9"), e("session-state"))

	once, err := client.Subscribe(login("admin", "admin"))
	if err != nil {
		t.Fatal(err)
	}
	_ = once.Send(&gpb.SubscribeRequest{Request: &gpb.SubscribeRequest_Subscribe{Subscribe: &gpb.SubscriptionList{
		Mode: gpb.SubscriptionList_ONCE, Subscription: []*gpb.Subscription{{Path: path}}}}})
	first, err := once.Recv()
	if err != nil || first.GetUpdate().GetUpdate()[0].Val.GetStringVal() != "active" {
		t.Fatalf("once update: %v %v", first, err)
	}
	if sync, err := once.Recv(); err != nil || !sync.GetSyncResponse() {
		t.Fatalf("once sync: %v %v", sync, err)
	}

	ctx, cancel := context.WithTimeout(login("admin", "admin"), 20*time.Second)
	defer cancel()
	stream, err := client.Subscribe(ctx)
	if err != nil {
		t.Fatal(err)
	}
	_ = stream.Send(&gpb.SubscribeRequest{Request: &gpb.SubscribeRequest_Subscribe{Subscribe: &gpb.SubscriptionList{
		Mode: gpb.SubscriptionList_STREAM, Subscription: []*gpb.Subscription{{Path: path, Mode: gpb.SubscriptionMode_ON_CHANGE}}}}})
	if _, err := stream.Recv(); err != nil { // initial value
		t.Fatal(err)
	}
	if sync, _ := stream.Recv(); !sync.GetSyncResponse() {
		t.Fatal("no sync_response")
	}
	data.set("tarvos-bgp:bgp", decode(t, strings.Replace(bgpJSON, `"session-state": "active"`, `"session-state": "established"`, 1)))
	changed, err := stream.Recv() // within one Sysrepo poll (5 s)
	if err != nil {
		t.Fatal(err)
	}
	if got := changed.GetUpdate().GetUpdate(); len(got) != 1 || got[0].Val.GetStringVal() != "established" {
		t.Fatalf("on_change: %v", changed)
	}
}

func TestChangesDeletes(t *testing.T) {
	sub := &subscription{}
	mk := func(v string) *gpb.Update {
		return &gpb.Update{Path: p(e("a"), e("b", "k", "x/y")), Val: &gpb.TypedValue{Value: &gpb.TypedValue_StringVal{StringVal: v}}}
	}
	changes(sub, map[string]*gpb.Update{"/a/b[k=x/y]": mk("1")}, true)
	updates, deletes := changes(sub, map[string]*gpb.Update{}, false)
	if len(updates) != 0 || len(deletes) != 1 || PathString(deletes[0].Elem) != "/a/b[k=x/y]" {
		t.Fatalf("deletes: %v %v", updates, deletes)
	}
}
