#!/usr/bin/env bash
# Two-leaf BGP EVPN/VXLAN lab configured only over NETCONF (tests/integration/netconf_evpn.py).
#
#   h1 --lan1-- [eth2] leaf1 [eth1] --fabric-- [eth1] leaf2 [eth2] --lan2-- h2
#
# Proves: iBGP underlay + EVPN session, type-3 (IMET), type-2 (MAC/IP, L2 stretch of
# VNI 100 between h1 and h2), type-5 (IP prefix of VRF RED) with symmetric IRB over
# L3VNI 5000, and clean removal of everything over NETCONF.
# Usage: tests/integration/evpn.sh <image>
set -euo pipefail

IMAGE="${1:?usage: evpn.sh <image>}"
ID="$$"
NETS=("tvfab-${ID}" "tvlan1-${ID}" "tvlan2-${ID}")
CONTAINERS=("leaf1-${ID}" "leaf2-${ID}" "h1-${ID}" "h2-${ID}")
HERE="$(dirname "$0")"

cleanup() {
    status=$?
    if [ "${status}" -ne 0 ]; then
        for c in "${CONTAINERS[@]:0:2}"; do
            echo "---- ${c}" >&2
            docker logs "${c}" 2>&1 | grep -E 'frr-sync|netdev-sync|Traceback' | tail -20 >&2 || true
            docker exec "${c}" vtysh -c 'show running-config no-header' >&2 2>/dev/null || true
        done
    fi
    if [ -n "${KEEP:-}" ]; then
        echo "KEEP set: lab left running (${CONTAINERS[*]})" >&2
        return
    fi
    docker rm -f "${CONTAINERS[@]}" >/dev/null 2>&1 || true
    for n in "${NETS[@]}"; do docker network rm "${n}" >/dev/null 2>&1 || true; done
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

# wait_for <seconds> <description> <command...>: retry until the command succeeds.
wait_for() {
    local seconds="$1" what="$2"
    shift 2
    for _ in $(seq 1 "${seconds}"); do
        "$@" >/dev/null 2>&1 && return 0
        sleep 1
    done
    fail "${what}"
}

docker network create --subnet 172.30.0.0/24 "${NETS[0]}" >/dev/null
docker network create --subnet 172.31.1.0/24 "${NETS[1]}" >/dev/null
docker network create --subnet 172.31.2.0/24 "${NETS[2]}" >/dev/null

for n in 1 2; do
    leaf="leaf${n}-${ID}"
    docker run -d --name "${leaf}" --hostname "leaf${n}" \
        --cap-add NET_ADMIN --cap-add NET_RAW --cap-add SYS_ADMIN \
        --sysctl net.ipv4.ip_forward=1 \
        --sysctl net.ipv4.tcp_l3mdev_accept=1 --sysctl net.ipv4.udp_l3mdev_accept=1 \
        -p "127.0.0.1::830" -e "FRR_DAEMONS=bgpd" "${IMAGE}" >/dev/null
    # Links are attached after boot, as containerlab does: eth1 fabric, eth2 LAN.
    docker network connect --ip "172.30.0.1${n}" "${NETS[0]}" "${leaf}"
    docker network connect "${NETS[n]}" "${leaf}"
    # A routed "server" subnet of VRF RED (red1 is configured over NETCONF).
    docker exec "${leaf}" sh -c 'ip link add red1 type dummy && ip link set red1 up'

    host="h${n}-${ID}"
    docker run -d --name "${host}" --network "${NETS[n]}" --cap-add NET_ADMIN alpine:3.20 sleep 3600 >/dev/null
    docker exec "${host}" sh -c "ip addr add 10.1.100.1${n}/24 dev eth0 && ip route add 10.10.0.0/16 via 10.1.100.1"
done

# Preflight: the host kernel must provide VRF and VXLAN devices (modules vrf, vxlan).
if ! docker exec "leaf1-${ID}" sh -c 'ip link add tvprobe0 type vrf table 4242 &&
        ip link add tvprobe1 type vxlan id 4242 local 127.0.0.1 dstport 4789 &&
        ip link del tvprobe1 && ip link del tvprobe0' 2>/dev/null; then
    echo "::warning::host kernel lacks VRF/VXLAN support (modprobe vrf vxlan): EVPN lab skipped"
    echo "SKIP: host kernel lacks VRF/VXLAN support"
    exit 0
fi

port() { docker port "leaf$1-${ID}" 830/tcp | head -n1 | sed 's/.*://'; }
vty() { docker exec "leaf$1-${ID}" vtysh -c "$2"; }

for n in 1 2; do
    wait_for 90 "leaf${n} frr-sync did not start" \
        sh -c "docker logs leaf${n}-${ID} 2>&1 | grep -q 'frr-sync\] serving'"
done

echo "== configure both leaves over NETCONF (one edit-config each)"
for n in 1 2; do
    python3 "${HERE}/netconf_evpn.py" set 127.0.0.1 "$(port "${n}")" admin admin "${n}" ||
        fail "leaf${n} configuration refused"
done

echo "== kernel devices created from tarvos-vrf / tarvos-vxlan"
for dev in RED br100 vx100 br5000 vx5000; do
    docker exec "leaf1-${ID}" ip link show "${dev}" >/dev/null || fail "leaf1: ${dev} missing"
done
docker exec "leaf1-${ID}" ip -d link show vx100 | grep -q 'vxlan id 100 local 10.255.0.1' ||
    fail "leaf1: vx100 has the wrong VXLAN parameters"
wait_for 20 "leaf1: eth2 not attached to br100" \
    sh -c "docker exec leaf1-${ID} ip link show eth2 | grep -q 'master br100'"

echo "== BGP session (underlay + EVPN)"
wait_for 60 "BGP session leaf1-leaf2 not established" \
    sh -c "docker exec leaf1-${ID} vtysh -c 'show bgp neighbors 172.30.0.12 json' | grep -q '\"bgpState\":\"Established\"'"
wait_for 30 "leaf2 loopback not learned by leaf1" \
    sh -c "docker exec leaf1-${ID} vtysh -c 'show ip route 10.255.0.2/32' | grep -q 'bgp'"

echo "== EVPN type-3 (remote VTEP of VNI 100)"
wait_for 30 "leaf1 has no remote VTEP for VNI 100" \
    sh -c "docker exec leaf1-${ID} vtysh -c 'show evpn vni 100' | grep -q '10.255.0.2'"

echo "== L2 over VXLAN: h1 -> h2 (10.1.100.12), then EVPN type-2 for h2"
wait_for 30 "h1 cannot ping h2 across VNI 100" docker exec "h1-${ID}" ping -c1 -W1 10.1.100.12
h2_mac="$(docker exec "h2-${ID}" cat /sys/class/net/eth0/address)"
wait_for 30 "leaf1 has no type-2 route for h2 (${h2_mac})" \
    sh -c "docker exec leaf1-${ID} vtysh -c 'show bgp l2vpn evpn route type macip' | grep -qi '${h2_mac}'"

echo "== EVPN type-5: leaf2's 10.10.2.0/24 in VRF RED on leaf1, routed over L3VNI 5000"
wait_for 30 "leaf1 has no type-5 route for 10.10.2.0/24" \
    sh -c "docker exec leaf1-${ID} vtysh -c 'show bgp l2vpn evpn route type prefix' | grep -q '10.10.2.0'"
wait_for 30 "10.10.2.0/24 not in leaf1 VRF RED" \
    sh -c "docker exec leaf1-${ID} vtysh -c 'show ip route vrf RED 10.10.2.0/24' | grep -q 'bgp'"
wait_for 30 "h1 cannot reach 10.10.2.1 (symmetric IRB)" docker exec "h1-${ID}" ping -c1 -W1 10.10.2.1

echo "== NETCONF operational data from mgmtd (frr-vrf state)"
python3 - "$(port 1)" <<'EOF' || fail "no frr-vrf operational data over NETCONF"
import sys
from ncclient import manager
with manager.connect(host="127.0.0.1", port=int(sys.argv[1]), username="admin", password="admin",
                     hostkey_verify=False, look_for_keys=False, allow_agent=False, timeout=30) as m:
    reply = m.get(filter=("subtree", '<lib xmlns="http://frrouting.org/yang/vrf"/>')).xml
sys.exit(0 if "<name>RED</name>" in reply and "<state>" in reply else 1)
EOF

echo "== remove everything over NETCONF (leaf1)"
python3 "${HERE}/netconf_evpn.py" clear 127.0.0.1 "$(port 1)" admin admin 1 || fail "removal refused"
vty 1 'show running-config no-header' | grep -Eq '^router bgp|^route-map|^ip prefix-list' &&
    fail "leaf1 still has Tarvos-owned FRR configuration"
for dev in RED br100 vx100 br5000 vx5000; do
    docker exec "leaf1-${ID}" ip link show "${dev}" >/dev/null 2>&1 && fail "leaf1: ${dev} not removed"
done
echo "PASS (EVPN type-2/type-3/type-5 over VXLAN, configured and removed over NETCONF)"
