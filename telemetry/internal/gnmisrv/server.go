package gnmisrv

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log"
	"sort"
	"strings"
	"sync"
	"time"

	gpb "github.com/openconfig/gnmi/proto/gnmi"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"
)

// GNMIVersion is the gNMI specification version implemented.
const GNMIVersion = "0.10.0"

const (
	minSample      = time.Second
	defaultSample  = 10 * time.Second
	localPoll      = time.Second     // ON_CHANGE detection, agent trees
	sysrepoPoll    = 5 * time.Second // ON_CHANGE detection, Sysrepo trees
	authCacheTTL   = time.Minute
	maxPathsPerRPC = 64
)

// Authenticator checks a user's password (PAM in production).
type Authenticator interface {
	Authenticate(user, password, remote string) error
}

// Server implements gnmi.GNMIServer.
type Server struct {
	gpb.UnimplementedGNMIServer
	Data  Data
	Index Index
	Auth  Authenticator
	// Allowed tells whether an authenticated user may read (TACACS_NETCONF_ACCESS).
	Allowed func(user string) bool

	mu    sync.Mutex
	cache map[string]time.Time // sha256(user, password) -> expiry
}

// Register installs the server and its authentication on a gRPC server.
func (s *Server) Register(g *grpc.Server) { gpb.RegisterGNMIServer(g, s) }

// Interceptors returns the gRPC options enforcing username/password metadata.
func (s *Server) Interceptors() []grpc.ServerOption {
	return []grpc.ServerOption{
		grpc.UnaryInterceptor(func(ctx context.Context, req any, _ *grpc.UnaryServerInfo, h grpc.UnaryHandler) (any, error) {
			if err := s.authenticate(ctx); err != nil {
				return nil, err
			}
			return h(ctx, req)
		}),
		grpc.StreamInterceptor(func(srv any, ss grpc.ServerStream, _ *grpc.StreamServerInfo, h grpc.StreamHandler) error {
			if err := s.authenticate(ss.Context()); err != nil {
				return err
			}
			return h(srv, ss)
		}),
	}
}

func (s *Server) authenticate(ctx context.Context) error {
	md, _ := metadata.FromIncomingContext(ctx)
	first := func(k string) string {
		if v := md.Get(k); len(v) > 0 {
			return v[0]
		}
		return ""
	}
	user, password := first("username"), first("password")
	if user == "" || password == "" {
		return status.Error(codes.Unauthenticated, "username and password metadata required")
	}
	sum := sha256.Sum256([]byte(user + "\x00" + password))
	token := hex.EncodeToString(sum[:])
	s.mu.Lock()
	if s.cache == nil {
		s.cache = map[string]time.Time{}
	}
	expiry, cached := s.cache[token]
	s.mu.Unlock()
	if !cached || time.Now().After(expiry) {
		remote := ""
		if p, ok := peer.FromContext(ctx); ok {
			remote = p.Addr.String()
			if i := strings.LastIndexByte(remote, ':'); i > 0 {
				remote = strings.Trim(remote[:i], "[]")
			}
		}
		if err := s.Auth.Authenticate(user, password, remote); err != nil {
			log.Printf("gNMI: authentication failed for %q from %s", user, remote)
			return status.Error(codes.Unauthenticated, "authentication failed")
		}
		s.mu.Lock()
		now := time.Now()
		for k, exp := range s.cache { // drop expired logins
			if now.After(exp) {
				delete(s.cache, k)
			}
		}
		s.cache[token] = now.Add(authCacheTTL)
		s.mu.Unlock()
	}
	if s.Allowed != nil && !s.Allowed(user) {
		return status.Error(codes.PermissionDenied, "gNMI access denied for this user")
	}
	return nil
}

// Capabilities implements gNMI Capabilities.
func (s *Server) Capabilities(context.Context, *gpb.CapabilityRequest) (*gpb.CapabilityResponse, error) {
	models := []*gpb.ModelData{
		{Name: "openconfig-interfaces", Organization: "OpenConfig working group", Version: "subset: state, counters"},
		{Name: "tarvos-telemetry", Organization: "Tarvos Network OS", Version: "2026-11-14"},
	}
	names := make([]string, 0, len(s.Index.Modules))
	for name := range s.Index.Modules {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		org := ""
		switch {
		case strings.HasPrefix(name, "tarvos-"):
			org = "Tarvos Network OS"
		case strings.HasPrefix(name, "frr-"):
			org = "FRRouting"
		case strings.HasPrefix(name, "ietf-"):
			org = "IETF"
		}
		models = append(models, &gpb.ModelData{Name: name, Organization: org, Version: s.Index.Modules[name]})
	}
	return &gpb.CapabilityResponse{
		SupportedModels:    models,
		SupportedEncodings: []gpb.Encoding{gpb.Encoding_JSON, gpb.Encoding_JSON_IETF, gpb.Encoding_PROTO},
		GNMIVersion:        GNMIVersion,
	}, nil
}

// Set is not supported: configuration is done over NETCONF.
func (s *Server) Set(context.Context, *gpb.SetRequest) (*gpb.SetResponse, error) {
	return nil, status.Error(codes.Unimplemented, "configure Tarvos over NETCONF (port 830); gNMI is read-only")
}

// join merges a prefix and a path.
func join(prefix, path *gpb.Path) (string, []*gpb.PathElem) {
	origin := path.GetOrigin()
	if origin == "" {
		origin = prefix.GetOrigin()
	}
	elems := append(append([]*gpb.PathElem{}, prefix.GetElem()...), path.GetElem()...)
	return origin, elems
}

// query resolves and selects one path.
func (s *Server) query(ctx context.Context, origin string, elems []*gpb.PathElem) ([]match, error) {
	if len(elems) == 0 {
		return nil, status.Error(codes.InvalidArgument, "empty path: name a top-level node, e.g. /interfaces")
	}
	root, err := s.Index.Resolve(origin, elems[0].Name)
	if err != nil {
		return nil, status.Error(codes.NotFound, err.Error())
	}
	tree, err := s.Data.Fetch(ctx, root)
	if err != nil {
		return nil, status.Error(codes.Unavailable, err.Error())
	}
	if tree == nil {
		return nil, nil
	}
	start := match{elems: []*gpb.PathElem{{Name: elems[0].Name}}, schema: "/" + string(root), value: tree}
	return Select(start, elems[1:], s.Index.Keys), nil
}

func notification(origin, target string, updates []*gpb.Update) *gpb.Notification {
	n := &gpb.Notification{Timestamp: time.Now().UnixNano(), Update: updates}
	if target != "" || origin != "" {
		n.Prefix = &gpb.Path{Origin: origin, Target: target}
	}
	return n
}

func (s *Server) leafUpdates(matches []match) ([]*gpb.Update, error) {
	var updates []*gpb.Update
	for _, m := range matches {
		for _, leaf := range Flatten(m, s.Index.Keys) {
			tv, err := TypedValue(leaf.Value)
			if err != nil {
				return nil, err
			}
			updates = append(updates, &gpb.Update{Path: &gpb.Path{Elem: leaf.Elems}, Val: tv})
		}
	}
	return updates, nil
}

// Get implements gNMI Get: JSON/JSON_IETF return one subtree per matched node, PROTO
// returns typed leaves.
func (s *Server) Get(ctx context.Context, req *gpb.GetRequest) (*gpb.GetResponse, error) {
	if len(req.GetPath()) > maxPathsPerRPC {
		return nil, status.Errorf(codes.InvalidArgument, "at most %d paths", maxPathsPerRPC)
	}
	encoding := req.GetEncoding()
	if encoding != gpb.Encoding_JSON && encoding != gpb.Encoding_JSON_IETF && encoding != gpb.Encoding_PROTO {
		return nil, status.Errorf(codes.Unimplemented, "encoding %s not supported (JSON, JSON_IETF, PROTO)", encoding)
	}
	resp := &gpb.GetResponse{}
	for _, path := range req.GetPath() {
		origin, elems := join(req.GetPrefix(), path)
		matches, err := s.query(ctx, origin, elems)
		if err != nil {
			return nil, err
		}
		var updates []*gpb.Update
		if encoding == gpb.Encoding_PROTO {
			if updates, err = s.leafUpdates(matches); err != nil {
				return nil, status.Error(codes.Internal, err.Error())
			}
		} else {
			for _, m := range matches {
				tv, err := JSONValue(m.value, encoding == gpb.Encoding_JSON_IETF)
				if err != nil {
					return nil, status.Error(codes.Internal, err.Error())
				}
				updates = append(updates, &gpb.Update{Path: &gpb.Path{Elem: m.elems}, Val: tv})
			}
		}
		resp.Notification = append(resp.Notification, notification(origin, req.GetPrefix().GetTarget(), updates))
	}
	return resp, nil
}

// subscription is one path of a SubscriptionList.
type subscription struct {
	origin   string
	elems    []*gpb.PathElem
	mode     gpb.SubscriptionMode
	interval time.Duration
	suppress bool
	poll     time.Duration // ON_CHANGE detection interval
	last     map[string]string
}

// collect returns the current leaves of a subscription (path string -> update).
func (s *Server) collect(ctx context.Context, sub *subscription) (map[string]*gpb.Update, error) {
	matches, err := s.query(ctx, sub.origin, sub.elems)
	if err != nil {
		return nil, err
	}
	updates, err := s.leafUpdates(matches)
	if err != nil {
		return nil, err
	}
	out := make(map[string]*gpb.Update, len(updates))
	for _, u := range updates {
		out[PathString(u.Path.Elem)] = u
	}
	return out, nil
}

// changes returns the updates that differ from the previous collection and the paths
// that disappeared, and remembers the new values.
func changes(sub *subscription, current map[string]*gpb.Update, all bool) ([]*gpb.Update, []*gpb.Path) {
	var updates []*gpb.Update
	var deletes []*gpb.Path
	next := make(map[string]string, len(current))
	keys := make([]string, 0, len(current))
	for k := range current {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		value := current[k].Val.String()
		next[k] = value
		if all || sub.last == nil || sub.last[k] != value {
			updates = append(updates, current[k])
		}
	}
	for k := range sub.last {
		if _, ok := next[k]; !ok {
			deletes = append(deletes, pathFromString(k, current))
		}
	}
	sub.last = next
	return updates, deletes
}

// pathFromString rebuilds a deleted path (kept as string): elements without keys are
// enough for clients to match the PathString form they received.
func pathFromString(k string, _ map[string]*gpb.Update) *gpb.Path {
	var elems []*gpb.PathElem
	for _, part := range splitPath(k) {
		name, keys := part, map[string]string{}
		if i := strings.IndexByte(part, '['); i >= 0 {
			name = part[:i]
			for _, kv := range strings.Split(strings.TrimSuffix(part[i+1:], "]"), "][") {
				if j := strings.IndexByte(kv, '='); j > 0 {
					keys[kv[:j]] = kv[j+1:]
				}
			}
		}
		e := &gpb.PathElem{Name: name}
		if len(keys) > 0 {
			e.Key = keys
		}
		elems = append(elems, e)
	}
	return &gpb.Path{Elem: elems}
}

// splitPath splits "/a/b[k=x/y]/c" on the slashes outside brackets.
func splitPath(p string) []string {
	var parts []string
	depth, start := 0, 1
	for i := 1; i < len(p); i++ {
		switch p[i] {
		case '[':
			depth++
		case ']':
			depth--
		case '/':
			if depth == 0 {
				parts = append(parts, p[start:i])
				start = i + 1
			}
		}
	}
	return append(parts, p[start:])
}

type sender struct {
	mu     sync.Mutex
	stream gpb.GNMI_SubscribeServer
}

func (s *sender) send(resp *gpb.SubscribeResponse) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.stream.Send(resp)
}

func (s *sender) update(n *gpb.Notification) error {
	return s.send(&gpb.SubscribeResponse{Response: &gpb.SubscribeResponse_Update{Update: n}})
}

func (s *sender) sync() error {
	return s.send(&gpb.SubscribeResponse{Response: &gpb.SubscribeResponse_SyncResponse{SyncResponse: true}})
}

// Subscribe implements gNMI Subscribe: ONCE, POLL, and STREAM with SAMPLE and
// ON_CHANGE (TARGET_DEFINED is SAMPLE every 10 s).
func (s *Server) Subscribe(stream gpb.GNMI_SubscribeServer) error {
	ctx := stream.Context()
	req, err := stream.Recv()
	if err != nil {
		return err
	}
	list := req.GetSubscribe()
	if list == nil {
		return status.Error(codes.InvalidArgument, "the first SubscribeRequest must carry a SubscriptionList")
	}
	if len(list.GetSubscription()) == 0 || len(list.GetSubscription()) > maxPathsPerRPC {
		return status.Errorf(codes.InvalidArgument, "1 to %d subscriptions", maxPathsPerRPC)
	}
	target := list.GetPrefix().GetTarget()
	var subs []*subscription
	for _, item := range list.GetSubscription() {
		origin, elems := join(list.GetPrefix(), item.GetPath())
		root := ""
		if len(elems) > 0 {
			r, err := s.Index.Resolve(origin, elems[0].Name)
			if err != nil {
				return status.Error(codes.NotFound, err.Error())
			}
			root = string(r)
		}
		sub := &subscription{origin: origin, elems: elems, mode: item.GetMode(), suppress: item.GetSuppressRedundant()}
		sub.interval = time.Duration(item.GetSampleInterval())
		if sub.mode == gpb.SubscriptionMode_TARGET_DEFINED {
			sub.mode = gpb.SubscriptionMode_SAMPLE
		}
		if sub.interval == 0 {
			sub.interval = defaultSample
		} else if sub.interval < minSample {
			sub.interval = minSample
		}
		sub.poll = sysrepoPoll
		if root == ocInterfaces || root == telemetry {
			sub.poll = localPoll
		}
		subs = append(subs, sub)
	}
	out := &sender{stream: stream}
	initial := func() error {
		for _, sub := range subs {
			current, err := s.collect(ctx, sub)
			if err != nil {
				return err
			}
			updates, _ := changes(sub, current, true)
			if list.GetUpdatesOnly() {
				continue
			}
			if err := out.update(notification(sub.origin, target, updates)); err != nil {
				return err
			}
		}
		return out.sync()
	}

	switch list.GetMode() {
	case gpb.SubscriptionList_ONCE:
		return initial()
	case gpb.SubscriptionList_POLL:
		if err := initial(); err != nil {
			return err
		}
		for {
			req, err := stream.Recv()
			if err != nil {
				return ignoreEOF(err)
			}
			if req.GetPoll() == nil {
				return status.Error(codes.InvalidArgument, "only Poll requests are allowed in POLL mode")
			}
			for _, sub := range subs {
				sub.last = nil
			}
			if err := initial(); err != nil {
				return err
			}
		}
	}

	// STREAM
	if err := initial(); err != nil {
		return err
	}
	errs := make(chan error, len(subs)+1)
	go func() { // the client may close its side; that ends the subscription
		for {
			if _, err := stream.Recv(); err != nil {
				errs <- ignoreEOF(err)
				return
			}
		}
	}()
	for _, sub := range subs {
		go func(sub *subscription) {
			period := sub.interval
			if sub.mode == gpb.SubscriptionMode_ON_CHANGE {
				period = sub.poll
			}
			ticker := time.NewTicker(period)
			defer ticker.Stop()
			for {
				select {
				case <-ctx.Done():
					return
				case <-ticker.C:
				}
				current, err := s.collect(ctx, sub)
				if err != nil {
					log.Printf("gNMI: subscription %s: %v", PathString(sub.elems), err)
					continue
				}
				all := sub.mode == gpb.SubscriptionMode_SAMPLE && !sub.suppress
				updates, deletes := changes(sub, current, all)
				if len(updates) == 0 && len(deletes) == 0 {
					continue
				}
				n := notification(sub.origin, target, updates)
				n.Delete = deletes
				if err := out.update(n); err != nil {
					errs <- err
					return
				}
			}
		}(sub)
	}
	select {
	case <-ctx.Done():
		return nil
	case err := <-errs:
		return err
	}
}

func ignoreEOF(err error) error {
	if errors.Is(err, io.EOF) || errors.Is(err, context.Canceled) || status.Code(err) == codes.Canceled {
		return nil
	}
	return err
}

// String is for logs.
func (s *subscription) String() string {
	return fmt.Sprintf("%s %s", PathString(s.elems), s.mode)
}
