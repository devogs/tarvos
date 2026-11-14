package main

import (
	"reflect"
	"testing"
)

func TestSplitList(t *testing.T) {
	for raw, want := range map[string][]string{
		"lo":             {"lo"},
		"lo, vrrp*":      {"lo", "vrrp*"},
		`["lo", "eth0"]`: {"lo", "eth0"},
		"[lo eth0]":      {"lo", "eth0"},
		"":               nil,
	} {
		if got := splitList(raw); !reflect.DeepEqual(got, want) {
			t.Errorf("%q: got %v", raw, got)
		}
	}
}
