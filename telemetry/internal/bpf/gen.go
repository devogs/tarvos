// Package bpf embeds the compiled eBPF object (telemetry/bpf/tarvos.bpf.c, GPL).
//
// The generated files are committed so that the Go code builds and tests without
// clang; the image build regenerates them from the C source (see docker/Dockerfile).
package bpf

//go:generate go tool bpf2go -tags linux -target bpfel -type if_key -type if_value -type cp_key -type cp_value -type drop_key Tarvos ../../bpf/tarvos.bpf.c -- -O2 -g -Wall -Werror -I/usr/include/x86_64-linux-gnu
