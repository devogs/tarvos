// SPDX-License-Identifier: GPL-2.0-only
/*
 * Tarvos kernel telemetry: per-interface traffic, packet-size histograms,
 * control-plane protocol counters (tc/TCX, every interface of the container) and
 * packet drops by reason (tp_btf/kfree_skb, filtered to the container's network
 * namespace). The Go agent (telemetry/internal/agent) owns these maps and is their only
 * reader. Kernel structures are accessed with CO-RE (preserve_access_index), so no
 * vmlinux.h is needed and the object loads on any kernel with BTF.
 */
#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/in.h>
#include <linux/ip.h>
#include <linux/ipv6.h>
#include <linux/pkt_cls.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_tracing.h>

char LICENSE[] SEC("license") = "GPL";

#define DIR_RX 0
#define DIR_TX 1
#define SIZE_BUCKETS 8 /* <=64 <=128 <=256 <=512 <=1024 <=1518 <=9216 >9216 */
#define MAX_INTERFACES 1024

/* Control-plane protocols (keep in sync with telemetry/internal/agent/protocols.go). */
enum proto {
	P_BGP = 1,
	P_BFD,
	P_LDP,
	P_OSPF,
	P_ISIS,
	P_VRRP,
	P_PIM,
	P_LLDP,
	P_VXLAN,
	P_SSH,
	P_NETCONF,
	P_TACACS,
	P_GNMI,
	P_PROMETHEUS,
};

struct if_key {
	__u32 ifindex;
	__u32 dir;
};

struct if_value {
	__u64 packets;
	__u64 bytes;
	__u64 size[SIZE_BUCKETS];
};

struct cp_key {
	__u32 ifindex;
	__u16 proto;
	__u16 dir;
};

struct cp_value {
	__u64 packets;
	__u64 bytes;
};

struct drop_key {
	__u32 ifindex;
	__u32 reason;
};

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_HASH);
	__uint(max_entries, MAX_INTERFACES * 2);
	__type(key, struct if_key);
	__type(value, struct if_value);
} if_stats SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_HASH);
	__uint(max_entries, MAX_INTERFACES * 8);
	__type(key, struct cp_key);
	__type(value, struct cp_value);
} cp_stats SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_HASH);
	__uint(max_entries, MAX_INTERFACES * 8);
	__type(key, struct drop_key);
	__type(value, __u64);
} drop_stats SEC(".maps");

/* Network namespace inode of the container, set by the agent at load time. */
volatile const __u32 target_netns = 0;

static __always_inline __u32 size_bucket(__u32 len)
{
	if (len <= 64)
		return 0;
	if (len <= 128)
		return 1;
	if (len <= 256)
		return 2;
	if (len <= 512)
		return 3;
	if (len <= 1024)
		return 4;
	if (len <= 1518)
		return 5;
	if (len <= 9216)
		return 6;
	return 7;
}

static __always_inline int port_proto(__u16 sport, __u16 dport, __u8 l4)
{
	__u16 ports[2] = { sport, dport };

#pragma unroll
	for (int i = 0; i < 2; i++) {
		__u16 p = ports[i];

		if (l4 == IPPROTO_TCP) {
			if (p == 179)
				return P_BGP;
			if (p == 646)
				return P_LDP;
			if (p == 22)
				return P_SSH;
			if (p == 830)
				return P_NETCONF;
			if (p == 49)
				return P_TACACS;
			if (p == 57400)
				return P_GNMI;
			if (p == 9273)
				return P_PROMETHEUS;
		} else if (l4 == IPPROTO_UDP) {
			if (p == 3784 || p == 4784)
				return P_BFD;
			if (p == 646)
				return P_LDP;
			if (p == 4789)
				return P_VXLAN;
		}
	}
	return 0;
}

static __always_inline int ip_proto(__u8 l4)
{
	switch (l4) {
	case 89:
		return P_OSPF;
	case 112:
		return P_VRRP;
	case 103:
		return P_PIM;
	}
	return 0;
}

/* Classify a frame starting at the Ethernet header (one 802.1Q tag allowed). */
static __always_inline int classify(struct __sk_buff *skb)
{
	__u32 off = ETH_HLEN;
	__u16 proto = bpf_ntohs(skb->protocol);
	__u8 l4 = 0;
	__u8 llc[3];

	if (proto == 0x8100 || proto == 0x88a8) {
		__be16 inner;

		if (bpf_skb_load_bytes(skb, ETH_HLEN + 2, &inner, sizeof(inner)) < 0)
			return 0;
		proto = bpf_ntohs(inner);
		off += 4;
	}
	if (proto == 0x88cc)
		return P_LLDP;
	if (proto < 0x0600) { /* 802.3 length field: LLC; IS-IS uses DSAP/SSAP 0xfe */
		if (bpf_skb_load_bytes(skb, off, llc, sizeof(llc)) < 0)
			return 0;
		return (llc[0] == 0xfe && llc[1] == 0xfe) ? P_ISIS : 0;
	}
	if (proto == ETH_P_IP) {
		struct iphdr ip;

		if (bpf_skb_load_bytes(skb, off, &ip, sizeof(ip)) < 0)
			return 0;
		l4 = ip.protocol;
		off += ip.ihl * 4;
		if (ip.frag_off & bpf_htons(0x1fff)) /* not the first fragment: no L4 header */
			return ip_proto(l4);
	} else if (proto == ETH_P_IPV6) {
		struct ipv6hdr ip6;

		if (bpf_skb_load_bytes(skb, off, &ip6, sizeof(ip6)) < 0)
			return 0;
		l4 = ip6.nexthdr;
		off += sizeof(ip6);
	} else {
		return 0;
	}
	if (l4 == IPPROTO_TCP || l4 == IPPROTO_UDP) {
		__be16 ports[2];

		if (bpf_skb_load_bytes(skb, off, ports, sizeof(ports)) < 0)
			return 0;
		return port_proto(bpf_ntohs(ports[0]), bpf_ntohs(ports[1]), l4);
	}
	return ip_proto(l4);
}

static __always_inline void count(struct __sk_buff *skb, __u32 dir)
{
	struct if_key key = { .ifindex = skb->ifindex, .dir = dir };
	struct if_value *value = bpf_map_lookup_elem(&if_stats, &key);
	__u32 bucket = size_bucket(skb->len);

	if (!value) {
		struct if_value zero = {};

		bpf_map_update_elem(&if_stats, &key, &zero, BPF_NOEXIST);
		value = bpf_map_lookup_elem(&if_stats, &key);
		if (!value)
			return;
	}
	/* Per-CPU values: plain increments are safe. */
	value->packets++;
	value->bytes += skb->len;
	if (bucket < SIZE_BUCKETS)
		value->size[bucket]++;

	/* The agent only attaches to devices with an Ethernet header (not VRF masters). */
	int proto = classify(skb);

	if (proto) {
		struct cp_key ckey = { .ifindex = skb->ifindex, .proto = proto, .dir = dir };
		struct cp_value *cval = bpf_map_lookup_elem(&cp_stats, &ckey);

		if (!cval) {
			struct cp_value czero = {};

			bpf_map_update_elem(&cp_stats, &ckey, &czero, BPF_NOEXIST);
			cval = bpf_map_lookup_elem(&cp_stats, &ckey);
			if (!cval)
				return;
		}
		cval->packets++;
		cval->bytes += skb->len;
	}
}

SEC("tc")
int tarvos_ingress(struct __sk_buff *skb)
{
	count(skb, DIR_RX);
	return TC_ACT_UNSPEC; /* TCX_NEXT: never alter forwarding */
}

SEC("tc")
int tarvos_egress(struct __sk_buff *skb)
{
	count(skb, DIR_TX);
	return TC_ACT_UNSPEC;
}

/* Minimal kernel types, relocated against the running kernel's BTF (CO-RE). */
struct ns_common {
	unsigned int inum;
} __attribute__((preserve_access_index));

struct net {
	struct ns_common ns;
} __attribute__((preserve_access_index));

typedef struct {
	struct net *net;
} __attribute__((preserve_access_index)) possible_net_t;

struct net_device {
	int ifindex;
	possible_net_t nd_net;
} __attribute__((preserve_access_index));

struct sk_buff {
	struct net_device *dev;
} __attribute__((preserve_access_index));

SEC("tp_btf/kfree_skb")
int BPF_PROG(tarvos_kfree_skb, struct sk_buff *skb, void *location, int reason)
{
	struct net_device *dev = BPF_CORE_READ(skb, dev);
	struct drop_key key = {};
	__u64 one = 1, *value;

	if (!dev)
		return 0;
	if (BPF_CORE_READ(dev, nd_net.net, ns.inum) != target_netns)
		return 0;
	key.ifindex = BPF_CORE_READ(dev, ifindex);
	key.reason = reason;
	value = bpf_map_lookup_elem(&drop_stats, &key);
	if (value)
		(*value)++;
	else
		bpf_map_update_elem(&drop_stats, &key, &one, BPF_NOEXIST);
	return 0;
}
