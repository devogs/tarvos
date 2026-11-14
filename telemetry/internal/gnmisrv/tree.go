// Package gnmisrv is the Tarvos gNMI server: Capabilities, Get and Subscribe over the
// telemetry agent (OpenConfig interface counters, tarvos-telemetry) and the Sysrepo
// operational datastore (every other module, including BGP/EVPN state).
//
// Data is handled as RFC 7951-style JSON trees (map[string]any / []any): the gNMI path
// selects nodes in the tree, which are returned as JSON subtrees or flattened into
// typed leaves. List keys come from an index of the YANG schema (scripts/yang_index.py)
// for Sysrepo data, and from Index for the trees built here.
package gnmisrv

import (
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"

	gpb "github.com/openconfig/gnmi/proto/gnmi"
)

// Keys maps the data path of a list (RFC 7951 member names, module prefix on the
// top node and where the module changes: "/tarvos-bgp:bgp/state/instance") to its keys.
type Keys map[string][]string

// match is a node selected by a path: its concrete path (list keys filled in), the
// schema path used to look up keys below it, and its value.
type match struct {
	elems  []*gpb.PathElem
	schema string
	value  any
}

// memberName tells whether a JSON member matches a path element name: exact, or the
// same name with or without a module prefix ("frr-zebra:zebra" matches "zebra").
func memberName(member, name string) bool {
	if member == name {
		return true
	}
	local := func(s string) string {
		if i := strings.IndexByte(s, ':'); i >= 0 {
			return s[i+1:]
		}
		return s
	}
	return local(member) == local(name) && (!strings.Contains(name, ":") || !strings.Contains(member, ":"))
}

// scalar renders a JSON scalar as a gNMI key value.
func scalar(v any) (string, bool) {
	switch x := v.(type) {
	case string:
		return x, true
	case json.Number:
		return x.String(), true
	case bool:
		return strconv.FormatBool(x), true
	case float64:
		return strconv.FormatFloat(x, 'f', -1, 64), true
	case uint64:
		return strconv.FormatUint(x, 10), true
	case uint32:
		return strconv.FormatUint(uint64(x), 10), true
	case int:
		return strconv.Itoa(x), true
	case int64:
		return strconv.FormatInt(x, 10), true
	}
	return "", false
}

// entryKeys returns the key values of a list entry ("" if one is missing).
func entryKeys(entry map[string]any, names []string) (map[string]string, bool) {
	out := map[string]string{}
	for _, name := range names {
		v, ok := scalar(entry[name])
		if !ok {
			return nil, false
		}
		out[name] = v
	}
	return out, true
}

func keyFilter(entry map[string]any, want map[string]string) bool {
	for k, v := range want {
		if v == "*" {
			continue
		}
		got, ok := scalar(entry[k])
		if !ok || got != v {
			return false
		}
	}
	return true
}

func copyElems(elems []*gpb.PathElem, extra ...*gpb.PathElem) []*gpb.PathElem {
	out := make([]*gpb.PathElem, 0, len(elems)+len(extra))
	out = append(out, elems...)
	return append(out, extra...)
}

// Select walks elems (below the root element, which the caller resolved) from root.
func Select(root match, elems []*gpb.PathElem, keys Keys) []match {
	current := []match{root}
	for _, elem := range elems {
		var next []match
		for _, m := range current {
			obj, ok := m.value.(map[string]any)
			if !ok {
				continue
			}
			for member, value := range obj {
				if !memberName(member, elem.Name) {
					continue
				}
				schema := m.schema + "/" + member
				list, isList := value.([]any)
				if !isList || !isListNode(keys, schema, value) {
					if len(elem.Key) == 0 {
						next = append(next, match{copyElems(m.elems, &gpb.PathElem{Name: member}), schema, value})
					}
					continue
				}
				names := keys[schema]
				for _, item := range list {
					entry, ok := item.(map[string]any)
					if !ok || !keyFilter(entry, elem.Key) {
						continue
					}
					kv, ok := entryKeys(entry, names)
					if !ok {
						continue
					}
					next = append(next, match{copyElems(m.elems, &gpb.PathElem{Name: member, Key: kv}), schema, entry})
				}
			}
		}
		current = next
	}
	return current
}

// isListNode distinguishes a YANG list (array of objects) from a leaf-list.
func isListNode(keys Keys, schema string, value any) bool {
	if _, ok := keys[schema]; ok {
		return true
	}
	if list, ok := value.([]any); ok && len(list) > 0 {
		_, isObject := list[0].(map[string]any)
		return isObject
	}
	return false
}

// Leaf is one flattened value.
type Leaf struct {
	Elems []*gpb.PathElem
	Value any
}

// Flatten turns a selected node into its leaves (sorted by path for stable output).
func Flatten(m match, keys Keys) []Leaf {
	var out []Leaf
	var walk func(elems []*gpb.PathElem, schema string, v any)
	walk = func(elems []*gpb.PathElem, schema string, v any) {
		switch x := v.(type) {
		case map[string]any:
			for member, value := range x {
				child := schema + "/" + member
				if list, ok := value.([]any); ok && isListNode(keys, child, value) {
					names := keys[child]
					for _, item := range list {
						entry, ok := item.(map[string]any)
						if !ok {
							continue
						}
						kv, ok := entryKeys(entry, names)
						if !ok {
							continue
						}
						walk(copyElems(elems, &gpb.PathElem{Name: member, Key: kv}), child, entry)
					}
					continue
				}
				walk(copyElems(elems, &gpb.PathElem{Name: member}), child, value)
			}
		default:
			out = append(out, Leaf{Elems: elems, Value: v})
		}
	}
	walk(m.elems, m.schema, m.value)
	sort.Slice(out, func(i, j int) bool { return PathString(out[i].Elems) < PathString(out[j].Elems) })
	return out
}

// PathString renders elems as /a/b[k=v]/c (keys sorted).
func PathString(elems []*gpb.PathElem) string {
	var b strings.Builder
	for _, e := range elems {
		b.WriteByte('/')
		b.WriteString(e.Name)
		names := make([]string, 0, len(e.Key))
		for k := range e.Key {
			names = append(names, k)
		}
		sort.Strings(names)
		for _, k := range names {
			fmt.Fprintf(&b, "[%s=%s]", k, e.Key[k])
		}
	}
	return b.String()
}

// TypedValue converts a JSON value into a gNMI TypedValue.
func TypedValue(v any) (*gpb.TypedValue, error) {
	switch x := v.(type) {
	case string:
		return &gpb.TypedValue{Value: &gpb.TypedValue_StringVal{StringVal: x}}, nil
	case bool:
		return &gpb.TypedValue{Value: &gpb.TypedValue_BoolVal{BoolVal: x}}, nil
	case uint64:
		return &gpb.TypedValue{Value: &gpb.TypedValue_UintVal{UintVal: x}}, nil
	case uint32:
		return &gpb.TypedValue{Value: &gpb.TypedValue_UintVal{UintVal: uint64(x)}}, nil
	case int:
		return &gpb.TypedValue{Value: &gpb.TypedValue_IntVal{IntVal: int64(x)}}, nil
	case int64:
		return &gpb.TypedValue{Value: &gpb.TypedValue_IntVal{IntVal: x}}, nil
	case json.Number:
		if u, err := strconv.ParseUint(x.String(), 10, 64); err == nil {
			return &gpb.TypedValue{Value: &gpb.TypedValue_UintVal{UintVal: u}}, nil
		}
		if i, err := x.Int64(); err == nil {
			return &gpb.TypedValue{Value: &gpb.TypedValue_IntVal{IntVal: i}}, nil
		}
		f, err := x.Float64()
		if err != nil {
			return nil, err
		}
		return &gpb.TypedValue{Value: &gpb.TypedValue_DoubleVal{DoubleVal: f}}, nil
	case float64:
		return &gpb.TypedValue{Value: &gpb.TypedValue_DoubleVal{DoubleVal: x}}, nil
	case []any: // leaf-list
		elements := make([]*gpb.TypedValue, 0, len(x))
		for _, item := range x {
			tv, err := TypedValue(item)
			if err != nil {
				return nil, err
			}
			elements = append(elements, tv)
		}
		return &gpb.TypedValue{Value: &gpb.TypedValue_LeaflistVal{LeaflistVal: &gpb.ScalarArray{Element: elements}}}, nil
	case nil: // YANG empty type is [null] in RFC 7951
		return &gpb.TypedValue{Value: &gpb.TypedValue_StringVal{StringVal: ""}}, nil
	}
	return nil, fmt.Errorf("unsupported value %T", v)
}

// JSONValue encodes a subtree for the JSON / JSON_IETF encodings.
func JSONValue(v any, ietf bool) (*gpb.TypedValue, error) {
	data, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	if ietf {
		return &gpb.TypedValue{Value: &gpb.TypedValue_JsonIetfVal{JsonIetfVal: data}}, nil
	}
	return &gpb.TypedValue{Value: &gpb.TypedValue_JsonVal{JsonVal: data}}, nil
}
