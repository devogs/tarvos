#!/usr/bin/env bash
# Kernel backends and FRR daemon state, configured and read back over NETCONF only.
#
# Node A (one container):
#   - tarvos-qos: htb on a dummy port with a DSCP 46 class; DSCP-marked packets are
#     counted in that class; invalid policies are refused; removal restores the default;
#   - tarvos-nftables: CoPP table with a NETCONF accept rule and a rate-limited UDP rule;
#     their counters move with real traffic; disabling removes the table;
#   - tarvos-aaa: state is served; disabling the local fallback without a TACACS+
#     server is refused (lockout guard).
# Nodes B1/B2 (two containers on one link): PIM neighbours, LDP session, VRRP master and
#   backup, BFD session configured with tarvos-frr / frr-bfdd and verified from
#   /tarvos-frr:frr/state.
# Usage: tests/integration/state.sh <image>   (needs python3 + ncclient, docker)
set -euo pipefail

IMAGE="${1:?usage: state.sh <image>}"
ID="$$"
NODE="tvstate-${ID}"
NET="tvstate-net-${ID}"
PAIR=("tvstate-b1-${ID}" "tvstate-b2-${ID}")
HERE="$(dirname "$0")"
T="urn:tarvos:params:xml:ns:yang:"
NC="urn:ietf:params:xml:ns:netconf:base:1.0"

cleanup() {
    status=$?
    if [ "${status}" -ne 0 ]; then
        for c in "${NODE}" "${PAIR[@]}"; do
            echo "---- ${c}" >&2
            docker logs "${c}" 2>&1 | grep -E 'sync\]|Traceback|Error' | tail -20 >&2 || true
        done
    fi
    docker rm -f "${NODE}" "${PAIR[@]}" >/dev/null 2>&1 || true
    docker network rm "${NET}" >/dev/null 2>&1 || true
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

wait_for() { # seconds description command...
    local seconds="$1" what="$2"
    shift 2
    for _ in $(seq 1 "${seconds}"); do
        "$@" >/dev/null 2>&1 && return 0
        sleep 1
    done
    fail "${what}"
}

run_node() { # name [docker args...]
    local name="$1"
    shift
    docker run -d --name "${name}" --hostname "${name}" \
        --cap-add NET_ADMIN --cap-add NET_RAW --cap-add SYS_ADMIN \
        --sysctl net.ipv4.tcp_l3mdev_accept=1 --sysctl net.ipv4.udp_l3mdev_accept=1 \
        -p "127.0.0.1::830" "$@" "${IMAGE}" >/dev/null
}
port() { docker port "$1" 830/tcp | head -n1 | sed 's/.*://'; }
edit() { python3 "${HERE}/netconf_edit.py" 127.0.0.1 "$(port "$1")"; }       # stdin: XML
get() { python3 "${HERE}/netconf_get.py" 127.0.0.1 "$(port "$1")" "$2"; }    # subtree filter
# xml_value <xml> <python expression over `root`, with helper f(path)>: prints the result
xml_value() {
    python3 -c '
import sys, xml.etree.ElementTree as ET
root = ET.fromstring(sys.stdin.read().encode())
def f(path):
    """Text of every element matching a "/"-separated path of local names."""
    nodes = [root]
    for name in path.split("/"):
        nodes = [c for n in nodes for c in n.iter() if c.tag.split("}")[-1] == name]
    return [n.text for n in nodes]
print(eval(sys.argv[1]))' "$2" <<<"$1"
}

# --------------------------------------------------------------------------- node A
run_node "${NODE}"
wait_for 90 "node: backends did not start" sh -c \
    "docker logs ${NODE} 2>&1 | grep -q 'nft-sync\] serving' &&
     docker logs ${NODE} 2>&1 | grep -q 'qos-sync\] serving'"
docker exec "${NODE}" sh -c 'ip link add q0 type dummy && ip addr add 10.99.0.1/24 dev q0 &&
    ip link set q0 up'

echo "== tarvos-qos: htb with a DSCP 46 class on q0"
edit "${NODE}" <<EOF || fail "qos configuration refused"
<qos xmlns="${T}tarvos-qos"><interface><name>q0</name><qdisc>htb</qdisc><bandwidth>10000</bandwidth>
  <class><id>10</id><dscp>46</dscp><priority>0</priority><rate>2000</rate></class>
  <class><id>20</id><dscp>26</dscp></class>
</interface></qos>
EOF
docker exec "${NODE}" tc class show dev q0 | grep -q 'class htb 1:a ' || fail "tc class 1:a missing"
# 25 UDP packets marked DSCP 46 (TOS 0xb8) out of q0 (a dummy device: counted, then dropped)
docker exec "${NODE}" python3 -c '
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xb8)
for _ in range(25):
    s.sendto(b"tarvos", ("10.99.0.2", 9))'
QOS_FILTER="<qos xmlns=\"${T}tarvos-qos\"><interface><name>q0</name><state/><class><id>10</id><state/></class></interface></qos>"
reply="$(get "${NODE}" "${QOS_FILTER}")"
kind="$(xml_value "${reply}" 'f("root-qdisc/kind")[0]')"
[ "${kind}" = "htb" ] || fail "qos state: root qdisc ${kind}"
ef="$(xml_value "${reply}" 'f("class/state/packets")[0]')"
[ "${ef}" -ge 25 ] || fail "qos state: class 10 counted ${ef} packets, expected >= 25"
echo "   class 10 (EF) counted ${ef} packets"

echo "== tarvos-qos: invalid policies are refused with the backend's reason"
msg="$(edit "${NODE}" <<EOF
<qos xmlns="${T}tarvos-qos"><interface><name>q0</name><qdisc>htb</qdisc><bandwidth>1000</bandwidth>
  <class><id>30</id><rate>5000</rate></class></interface></qos>
EOF
)" && fail "over-subscribed htb accepted"
grep -q "exceed the bandwidth" <<<"${msg}" || fail "unexpected refusal: ${msg}"
docker exec "${NODE}" tc class show dev q0 | grep -q 'class htb 1:a ' ||
    fail "refused edit changed the kernel"

echo "== tarvos-qos: removal restores the kernel default"
edit "${NODE}" <<<"<qos xmlns=\"${T}tarvos-qos\" xmlns:nc=\"${NC}\" nc:operation=\"remove\"/>" ||
    fail "qos removal refused"
docker exec "${NODE}" tc qdisc show dev q0 | grep -q htb && fail "htb still on q0"

echo "== tarvos-nftables: CoPP counters"
edit "${NODE}" <<EOF || fail "nftables configuration refused"
<nftables xmlns="${T}tarvos-nftables"><copp>
  <rule><name>netconf</name><protocol>tcp</protocol><destination-port>830</destination-port></rule>
  <rule><name>junk</name><protocol>udp</protocol><destination-port>9999</destination-port>
    <action>rate-limit</action><max-pps>5</max-pps><burst>5</burst></rule>
</copp></nftables>
EOF
docker exec "${NODE}" nft list chain inet tarvos copp | grep -q 'tarvos:junk:excess' ||
    fail "CoPP table not installed"
node_ip="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "${NODE}")"
python3 -c '
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
for _ in range(200):
    s.sendto(b"x", (sys.argv[1], 9999))' "${node_ip}"
NFT_FILTER="<nftables xmlns=\"${T}tarvos-nftables\"><copp/></nftables>"
reply="$(get "${NODE}" "${NFT_FILTER}")"
[ "$(xml_value "${reply}" 'f("copp/state/active")[0]')" = "true" ] || fail "nft state: not active"
counts="$(xml_value "${reply}" '" ".join(f("rule/name") + f("rule/state/matched-packets") + f("rule/state/dropped-packets"))')"
read -r n1 n2 m1 m2 d1 d2 <<<"${counts}"
[ "${n1} ${n2}" = "netconf junk" ] || fail "nft state: rules ${n1} ${n2}"
[ "${m1}" -gt 0 ] || fail "nft state: no NETCONF packets matched"
[ "${m2}" -ge 150 ] && [ "${d2}" -gt 100 ] && [ "${d1}" -eq 0 ] ||
    fail "nft state: junk matched ${m2}, dropped ${d2} (netconf dropped ${d1})"
echo "   netconf matched ${m1}; junk matched ${m2}, dropped ${d2}"

echo "== tarvos-nftables: disabling removes the table"
edit "${NODE}" <<<"<nftables xmlns=\"${T}tarvos-nftables\"><copp><enabled>false</enabled></copp></nftables>" ||
    fail "disabling CoPP refused"
docker exec "${NODE}" nft list table inet tarvos >/dev/null 2>&1 && fail "CoPP table still installed"
[ "$(xml_value "$(get "${NODE}" "${NFT_FILTER}")" 'f("copp/state/active")[0]')" = "false" ] ||
    fail "nft state: still active"

echo "== tarvos-aaa: state, and the lockout guard"
reply="$(get "${NODE}" "<aaa xmlns=\"${T}tarvos-aaa\"><state/></aaa>")"
[ "$(xml_value "${reply}" 'f("state/tacacs-active")[0] + f("state/server-source")[0]')" = "falseenvironment" ] ||
    fail "aaa state: ${reply}"
[ "$(xml_value "${reply}" 'f("last-local-authentication/user")[0]')" = "admin" ] ||
    fail "aaa state: our own admin NETCONF login was not recorded"
msg="$(edit "${NODE}" <<<"<aaa xmlns=\"${T}tarvos-aaa\"><local-fallback><enabled>false</enabled></local-fallback></aaa>")" &&
    fail "local fallback disabled without a TACACS+ server"
grep -q "nobody could log in" <<<"${msg}" || fail "unexpected refusal: ${msg}"

# --------------------------------------------------------------------------- nodes B
echo "== tarvos-frr: PIM, LDP, VRRP and BFD between two nodes"
docker network create --subnet 172.28.7.0/24 "${NET}" >/dev/null
for n in 1 2; do
    run_node "${PAIR[n-1]}" -e "FRR_DAEMONS=pimd ldpd vrrpd bfdd"
    docker network connect --ip "172.28.7.1${n}" "${NET}" "${PAIR[n-1]}"
done
for n in 1 2; do
    node="${PAIR[n-1]}"
    wait_for 90 "${node}: frr-sync did not start" \
        sh -c "docker logs ${node} 2>&1 | grep -q 'frr-sync\] serving'"
    # VRRP needs a macvlan carrying the virtual MAC (kernel side, as containerlab would).
    docker exec "${node}" sh -c 'ip link add vrrp4-7 link eth1 type macvlan mode bridge &&
        ip link set vrrp4-7 address 00:00:5e:00:01:07 &&
        ip addr add 172.28.7.100/24 dev vrrp4-7 && ip link set vrrp4-7 up'
    edit "${node}" <<EOF || fail "${node}: tarvos-frr configuration refused"
<lib xmlns="http://frrouting.org/yang/interface"><interface><name>lo</name>
  <zebra xmlns="http://frrouting.org/yang/zebra"><ipv4-addrs><ip>10.7.7.${n}</ip><prefix-length>32</prefix-length></ipv4-addrs></zebra>
</interface></lib>
<bfdd xmlns="http://frrouting.org/yang/bfdd"><bfd><sessions><single-hop>
  <dest-addr>172.28.7.1$((3 - n))</dest-addr><interface>eth1</interface><vrf>default</vrf>
</single-hop></sessions></bfd></bfdd>
<frr xmlns="${T}tarvos-frr">
  <daemon><name>pimd</name><interface><name>eth1</name><line>ip pim</line></interface></daemon>
  <daemon><name>vrrpd</name><interface><name>eth1</name>
    <line>vrrp 7</line><line>vrrp 7 priority $((100 + n * 10))</line><line>vrrp 7 ip 172.28.7.100</line>
  </interface></daemon>
  <daemon><name>ldpd</name><block><header>mpls ldp</header>
    <line>router-id 10.7.7.${n}</line><line>address-family ipv4</line>
    <line> discovery transport-address 172.28.7.1${n}</line><line> interface eth1</line>
    <line> exit</line><line>exit-address-family</line>
  </block></daemon>
</frr>
EOF
done

FRR_FILTER="<frr xmlns=\"${T}tarvos-frr\"><state/></frr>"
frr_ready() {
    local reply
    reply="$(get "${PAIR[0]}" "${FRR_FILTER}")"
    [ "$(xml_value "${reply}" '" ".join(f("pim/neighbor/address") + f("ldp/neighbor/state") + f("vrrp/router/address-family/state") + f("bfd/peer/status"))')" = \
        "172.28.7.12 operational backup up" ]
}
wait_for 90 "tarvos-frr state on ${PAIR[0]} (PIM neighbour, LDP operational, VRRP backup, BFD up)" frr_ready
reply="$(get "${PAIR[1]}" "${FRR_FILTER}")"
[ "$(xml_value "${reply}" 'f("vrrp/router/address-family/state")[0]')" = "master" ] ||
    fail "${PAIR[1]} (priority 120) is not the VRRP master"
echo "PASS (tc, nftables, AAA and FRR daemon state over NETCONF)"
