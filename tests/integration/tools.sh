#!/usr/bin/env bash
# Troubleshooting toolbox: every tool runs, and ping, traceroute, tracepath, mtr, DNS
# lookups, curl, nc and tcpdump work in every VRF, as root, admin and the TACACS+ account.
#
#   peer-mgmt --bridge-- [eth0, VRF mgmt]   (Docker's default bridge, like the other suites)
#   peer-def  --def--  [eth1, default VRF]   node
#   peer-red  --red--  [eth2, VRF RED]            (created over NETCONF)
#   peer-x    --cust-- [eth3, VRF cust_42]        (created over NETCONF, arbitrary name)
#
# Each peer runs a DNS resolver and an HTTP server (peer_server.py) reachable from its
# own VRF only. Also proves VRF isolation: a tenant VRF never falls back to the main or
# management table (unreachable default of netdev_sync).
# Usage: tests/integration/tools.sh <image>
set -euo pipefail

IMAGE="${1:?usage: tools.sh <image>}"
ID="$$"
HERE="$(cd "$(dirname "$0")" && pwd)"
NODE="tools-${ID}"
declare -A SUBNET=([def]=172.31.21 [red]=172.31.22 [cust]=172.31.23)
declare -A PEER=([def]=172.31.21.100 [red]=172.31.22.100 [cust]=172.31.23.100)
NETS=()
PEERS=("peermgmt-${ID}")
for k in def red cust; do
    NETS+=("tvt${k}-${ID}")
    PEERS+=("peer${k}-${ID}")
done

cleanup() {
    status=$?
    if [ "${status}" -ne 0 ]; then
        docker logs "${NODE}" 2>&1 | grep -E 'netdev-sync|Traceback' | tail -20 >&2 || true
    fi
    if [ -n "${KEEP:-}" ]; then
        echo "KEEP set: lab left running (${NODE} ${PEERS[*]})" >&2
        return
    fi
    docker rm -f "${NODE}" "${PEERS[@]}" >/dev/null 2>&1 || true
    for n in "${NETS[@]}"; do docker network rm "${n}" >/dev/null 2>&1 || true; done
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

wait_for() {
    local seconds="$1" what="$2"
    shift 2
    for _ in $(seq 1 "${seconds}"); do
        "$@" >/dev/null 2>&1 && return 0
        sleep 1
    done
    fail "${what}"
}

# Connecting a network after boot can make Docker move the container's default gateway
# (and drop the management VRF's default route); eth0 stays on the default bridge, as in
# the other suites, and the tenant networks have no gateway role.
peer() {  # peer <key> [docker run network args...]
    local k="$1"
    shift
    docker run -d --name "peer${k}-${ID}" --cap-add NET_ADMIN "$@" -v "${HERE}:/t:ro" --entrypoint python3 \
        "${IMAGE}" /t/peer_server.py "${k}" "${k}.tarvos.test=10.99.0.${#k}" >/dev/null
}
peer mgmt
PEER[mgmt]="$(docker inspect -f '{{.NetworkSettings.IPAddress}}' "peermgmt-${ID}")"
for k in def red cust; do
    docker network create --subnet "${SUBNET[${k}]}.0/24" "tvt${k}-${ID}" >/dev/null
    peer "${k}" --network "tvt${k}-${ID}" --ip "${PEER[${k}]}"
done

docker run -d --name "${NODE}" --hostname tools \
    --cap-add NET_ADMIN --cap-add NET_RAW --cap-add SYS_ADMIN --cap-add BPF --cap-add PERFMON \
    --sysctl net.ipv4.tcp_l3mdev_accept=1 --sysctl net.ipv4.udp_l3mdev_accept=1 \
    --sysctl net.ipv4.ip_forward=1 \
    -p "127.0.0.1::830" -e "FRR_DAEMONS=" -e TELEMETRY_ENABLED=no "${IMAGE}" >/dev/null
for k in def red cust; do
    docker network connect "tvt${k}-${ID}" "${NODE}"
done
port="$(docker port "${NODE}" 830/tcp | head -n1 | sed 's/.*://')"
x() { docker exec "${NODE}" "$@"; }

wait_for 90 "netdev-sync did not start" \
    sh -c "docker logs ${NODE} 2>&1 | grep -q 'netdev-sync\] serving'"

# Kernel interface names follow the attach order (eth1 def, eth2 red, eth3 cust).
if ! x ip -o link show eth0 | grep -q 'master mgmt '; then
    echo "::warning::host kernel lacks VRF support: toolbox VRF checks skipped"
    echo "SKIP: eth0 not in VRF mgmt"
    exit 0
fi

echo "== tools are installed and run"
for cmd in "ping -V" "tracepath -V" "arping -V" "traceroute --version" "mtr --version" \
    "dig -v" "nslookup -version" "host -V" "tcpdump --version" "ethtool --version" \
    "curl --version" "iperf3 --version" "conntrack --version" "vrfexec --list"; do
    # shellcheck disable=SC2086
    x ${cmd} >/dev/null 2>&1 || fail "${cmd} does not run"
done
x sh -c 'nc -h 2>&1 | grep -q OpenBSD' || fail "nc (netcat-openbsd) missing"

echo "== create two tenant VRFs over NETCONF (RED, and an arbitrary name)"
python3 - "${port}" <<'EOF' || fail "tenant VRFs refused"
import sys
from ncclient import manager
cfg = """<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
<vrfs xmlns="urn:tarvos:params:xml:ns:yang:tarvos-vrf">
  <vrf><name>RED</name><table>1010</table><interface>eth2</interface></vrf>
  <vrf><name>cust_42</name><table>4242</table><interface>eth3</interface></vrf>
</vrfs></config>"""
with manager.connect(host="127.0.0.1", port=int(sys.argv[1]), username="admin", password="admin",
                     hostkey_verify=False, look_for_keys=False, allow_agent=False, timeout=30) as m:
    m.edit_config(target="running", config=cfg)
EOF
wait_for 20 "eth3 not enslaved to cust_42" sh -c "docker exec ${NODE} ip -o link show eth3 | grep -q 'master cust_42 '"
# Docker assigned the addresses before enslaving: the connected routes moved to the VRF.
x ip route show table 1010 | grep -q "${SUBNET[red]}.0/24" || fail "RED table has no connected route"

declare -A VRF=([mgmt]=mgmt [def]=default [red]=RED [cust]=cust_42)
declare -A DEV=([mgmt]=eth0 [def]=eth1 [red]=eth2 [cust]=eth3)

for user in root admin tacacs; do
    for k in mgmt def red cust; do
        vrf="${VRF[${k}]}" peer="${PEER[${k}]}"
        echo "== ${user}: VRF ${vrf} -> ${peer}"
        u() { docker exec -u "${user}" "${NODE}" vrfexec "${vrf}" "$@"; }
        u ping -c1 -W2 "${peer}" >/dev/null || fail "${user}/${vrf}: ping"
        u tracepath -n -m 3 "${peer}" | grep -q "${peer}" || fail "${user}/${vrf}: tracepath"
        u traceroute -n -w 2 -q 1 -m 3 "${peer}" | grep -q "${peer}" || fail "${user}/${vrf}: traceroute"
        u mtr -n -r -c 1 "${peer}" | grep -q "${peer}" || fail "${user}/${vrf}: mtr"
        [ "$(u dig +short +time=2 +tries=1 "@${peer}" "${k}.tarvos.test")" = "10.99.0.${#k}" ] ||
            fail "${user}/${vrf}: dig through the VRF's own resolver"
        u nslookup -timeout=2 "${k}.tarvos.test" "${peer}" | grep -q "10.99.0.${#k}" ||
            fail "${user}/${vrf}: nslookup"
        u host -W 2 "${k}.tarvos.test" "${peer}" | grep -q "10.99.0.${#k}" || fail "${user}/${vrf}: host"
        [ "$(u curl -s --max-time 5 "http://${peer}:8080/")" = "tarvos-peer ${k}" ] ||
            fail "${user}/${vrf}: curl"
        u nc -z -w 3 "${peer}" 8080 2>/dev/null || fail "${user}/${vrf}: nc"
        # tcpdump on the VRF's port sees the ping (the pcap group grants capture)
        out="$(docker exec -u "${user}" "${NODE}" sh -c "timeout 8 tcpdump -lni ${DEV[${k}]} -c 1 'icmp and host ${peer}' 2>/dev/null & sleep 2; vrfexec ${vrf} ping -c2 -W1 ${peer} >/dev/null; wait")"
        grep -q "${peer}" <<<"${out}" || fail "${user}/${vrf}: tcpdump on ${DEV[${k}]}: ${out}"
    done
    echo "== ${user}: the tools' own VRF options"
    red="${PEER[red]}"
    e() { docker exec -u "${user}" "${NODE}" "$@"; }
    e ping -c1 -W2 -I RED "${red}" >/dev/null 2>&1 || fail "${user}: ping -I RED"
    e traceroute -n -w 2 -q 1 -m 3 -i RED "${red}" | grep -q "${red}" || fail "${user}: traceroute -i RED"
    # (mtr has no working VRF option: --interface binds the probes but not their replies;
    # vrfexec is the way, checked above)
    [ "$(e curl -s --max-time 5 --interface RED "http://${red}:8080/")" = "tarvos-peer red" ] ||
        fail "${user}: curl --interface RED"
    e arping -c1 -w3 -I eth2 "${red}" >/dev/null || fail "${user}: arping -I eth2"
done

echo "== VRF isolation: no leak between VRFs, no fallback of tenants to main or mgmt"
for k in mgmt def cust; do
    if x vrfexec RED ping -c1 -W1 "${PEER[${k}]}" >/dev/null 2>&1; then
        fail "VRF RED reaches the ${k} network"
    fi
done
out="$(x vrfexec RED ping -c1 -W1 "${PEER[mgmt]}" 2>&1 || true)"
grep -Eqi 'unreachable|no route to host' <<<"${out}" || fail "RED lookup of a mgmt address did not hit the unreachable default: ${out}"
x ip route show table 1010 | grep -q '^unreachable default .*proto tarvos' ||
    fail "RED table has no Tarvos isolation route"
if x ping -c1 -W1 "${PEER[red]}" >/dev/null 2>&1; then
    fail "default VRF reaches the RED network"
fi
# Forwarded traffic: a host of RED routing through the node towards the management
# network must be dropped (without the isolation route, the lookup that misses in RED's
# table falls through to main and to the mgmt fallback rule, and leaves on eth0).
node_red="$(docker exec "${NODE}" ip -4 -o addr show eth2 | awk '{print $4}' | cut -d/ -f1)"
node_mgmt="$(docker inspect -f '{{.NetworkSettings.IPAddress}}' "${NODE}")"
docker exec "peerred-${ID}" ip route add "${PEER[mgmt]}/32" via "${node_red}"
docker exec "peermgmt-${ID}" ip route add "${SUBNET[red]}.0/24" via "${node_mgmt}"
# The node must answer "no route" at once (ICMP unreachable, errno 113); a timeout means
# the packet was forwarded out of the management interface.
leak="$(docker exec "peerred-${ID}" python3 -c "
import socket
try:
    socket.create_connection(('${PEER[mgmt]}', 8080), timeout=3)
    print('connected')
except OSError as exc:
    print(type(exc).__name__, getattr(exc, 'errno', None))" 2>&1)"
[ "${leak}" = "OSError 113" ] ||
    fail "a host of VRF RED is not refused by the node towards the management network: ${leak}"
# The reconciler restores the isolation route if it is removed behind Tarvos' back.
x ip route del unreachable default table 1010 metric 4278198272
wait_for 25 "isolation route not restored" sh -c "docker exec ${NODE} ip route show table 1010 | grep -q '^unreachable default'"

echo "== vrfexec: unknown VRF refused, nesting and 'default' restore the default VRF"
if x vrfexec NOPE true 2>/dev/null; then fail "vrfexec accepted an unknown VRF"; fi
# shellcheck disable=SC2016  # expanded by the inner shell
x vrfexec RED vrfexec default sh -c '[ -z "${TARVOS_VRF:-}" ] && [ -z "${LD_PRELOAD:-}" ]' ||
    fail "vrfexec default does not leave the VRF"

echo "== tenant VRF removed over NETCONF: its isolation route goes too"
python3 - "${port}" <<'EOF' || fail "VRF removal refused"
import sys
from ncclient import manager
cfg = """<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"
  xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0">
<vrfs xmlns="urn:tarvos:params:xml:ns:yang:tarvos-vrf" nc:operation="remove"/></config>"""
with manager.connect(host="127.0.0.1", port=int(sys.argv[1]), username="admin", password="admin",
                     hostkey_verify=False, look_for_keys=False, allow_agent=False, timeout=30) as m:
    m.edit_config(target="running", config=cfg)
EOF
x ip route show table 1010 | grep -q unreachable && fail "isolation route of RED left behind"
echo "== management VRF keeps its default route when Docker connects a network after boot"
# eth0 on a user-defined network, then two more networks: Docker moves the container's
# default gateway (to the alphabetically first network) and removes the management
# default route; netdev_sync restores it (v2.1.1 fix), so NETCONF keeps answering.
NODE2="tools2-${ID}"
docker run -d --name "${NODE2}" --network "tvtdef-${ID}" \
    --cap-add NET_ADMIN --cap-add NET_RAW --cap-add SYS_ADMIN \
    --sysctl net.ipv4.tcp_l3mdev_accept=1 --sysctl net.ipv4.udp_l3mdev_accept=1 \
    -p "127.0.0.1::830" -e "FRR_DAEMONS=" -e TELEMETRY_ENABLED=no -e LLDP_ENABLED=no \
    "${IMAGE}" >/dev/null
PEERS+=("${NODE2}")
wait_for 90 "${NODE2}: netdev-sync did not start" \
    sh -c "docker logs ${NODE2} 2>&1 | grep -q 'netdev-sync\] serving'"
docker network connect "tvtred-${ID}" "${NODE2}"
docker network connect "tvtcust-${ID}" "${NODE2}"
wait_for 30 "${NODE2}: management default route not restored" \
    sh -c "docker exec ${NODE2} ip route show table 1001 | grep -q '^default via ${SUBNET[def]}.1 '"
port2="$(docker port "${NODE2}" 830/tcp | head -n1 | sed 's/.*://')"
python3 "${HERE}/netconf_caps.py" 127.0.0.1 "${port2}" admin admin tarvos-vrf >/dev/null ||
    fail "${NODE2}: NETCONF unreachable after a network was connected"

echo "PASS (toolbox in default, mgmt and two tenant VRFs, as root, admin and tacacs)"
