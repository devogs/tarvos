#!/usr/bin/env bash
# Repeatable Tarvos lab: one spine, two leaves, two hosts, a TACACS+ server and a syslog
# collector, wired like containerlab does (links attached after boot).
#
#             +---------- spine (RR, AS 65000, lo 10.255.0.254) ----------+
#             | eth1 10.0.1.2/29                         eth2 10.0.2.2/29 |
#             | eth1 10.0.1.3/29                         eth1 10.0.2.3/29 |
#   h1 --eth2-- leaf1 (lo 10.255.0.1)               leaf2 (lo 10.255.0.2) --eth2-- h2
#  10.1.100.11      VNI 100 (anycast GW 10.1.100.1), L3VNI 5000, VRF RED      10.1.100.12
#
# eth0 of every node, the TACACS+ server and the collector share Docker's default bridge
# (the management network, VRF mgmt on the nodes).
#
# Interface names are pinned (driver option com.docker.network.endpoint.ifname, Docker
# 28+): without it, Docker may reorder eth0/eth1/eth2 on "docker restart" and the node
# comes back with its management and fabric ports swapped.
#
# Usage: tests/lab/lab.sh up [IMAGE]   start everything (does not configure)
#        tests/lab/lab.sh configure    push the fabric configuration over NETCONF
#        tests/lab/lab.sh relink NODE  recreate a node's dummy server port after a
#                                      docker restart (Docker re-attaches the networks)
#        tests/lab/lab.sh down         remove everything
#        tests/lab/lab.sh status       containers and addresses
# State (addresses, users file) lives in tests/lab/.state/ (LAB_STATE overrides it).
# LAB_PREFIX (default "tvlab") names every container and network.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
P="${LAB_PREFIX:-tvlab}"
STATE="${LAB_STATE:-${HERE}/.state}"
NODES=(spine leaf1 leaf2)
HOSTS=(h1 h2)
NETS=("${P}-s1l1" "${P}-s1l2" "${P}-lan1" "${P}-lan2")
CAPS=(--cap-add NET_ADMIN --cap-add NET_RAW --cap-add SYS_ADMIN --cap-add BPF --cap-add PERFMON)
SYSCTLS=(--sysctl net.ipv4.ip_forward=1 --sysctl net.ipv4.tcp_l3mdev_accept=1
    --sysctl net.ipv4.udp_l3mdev_accept=1)
TACACS_KEY=labkey

log() { echo "[lab] $*" >&2; }
ip_of() { docker inspect -f '{{.NetworkSettings.IPAddress}}' "$1"; }

# node_run <name> <image> [extra docker run args...]: a Tarvos node on the default bridge
node_run() {
    local name="$1" image="$2"
    shift 2
    docker run -d --name "${P}-${name}" --hostname "${name}" "${CAPS[@]}" "${SYSCTLS[@]}" \
        --network "name=bridge,driver-opt=com.docker.network.endpoint.ifname=eth0" \
        -e "FRR_DAEMONS=bgpd,bfdd" -e "TACACS_SERVER=${TACACS_IP}" -e "TACACS_SECRET=${TACACS_KEY}" \
        -e "SYSLOG_SERVER=${SYSLOG_IP}" -e "SYSLOG_PROTOCOL=udp" "$@" "${image}" >/dev/null
}

wait_ready() {  # wait_ready <container>: NETCONF backends serving
    for _ in $(seq 1 120); do
        if docker logs "$1" 2>&1 | grep -q 'frr-sync\] serving' &&
            docker logs "$1" 2>&1 | grep -q 'netdev-sync\] serving'; then
            return 0
        fi
        sleep 1
    done
    log "ERROR: $1 did not become ready"
    return 1
}

# link <node>: attach a node's fabric links in a fixed order (eth1, eth2, ...)
connect() {  # connect <ifname> <network> <container> [--ip ADDRESS]
    local ifname="$1" net="$2" container="$3"
    shift 3
    docker network connect --driver-opt "com.docker.network.endpoint.ifname=${ifname}" "$@" "${net}" "${container}"
}

# link <node>: attach a node's fabric links with fixed names (eth1, eth2, ...)
link() {
    case "$1" in
        spine)
            connect eth1 "${P}-s1l1" "${P}-spine" --ip 10.0.1.2
            connect eth2 "${P}-s1l2" "${P}-spine" --ip 10.0.2.2
            ;;
        leaf1)
            connect eth1 "${P}-s1l1" "${P}-leaf1" --ip 10.0.1.3
            connect eth2 "${P}-lan1" "${P}-leaf1"
            docker exec "${P}-leaf1" sh -c 'ip link add red1 type dummy && ip link set red1 up'
            ;;
        leaf2)
            connect eth1 "${P}-s1l2" "${P}-leaf2" --ip 10.0.2.3
            connect eth2 "${P}-lan2" "${P}-leaf2"
            docker exec "${P}-leaf2" sh -c 'ip link add red1 type dummy && ip link set red1 up'
            ;;
    esac
}

up() {
    local image="${1:-tarvos:dev}"
    mkdir -p "${STATE}"
    printf 'alice:alicepw\nbob:bobpw\n' >"${STATE}/tacacs-users"
    chmod 0644 "${STATE}/tacacs-users"
    docker network create --subnet 10.0.1.0/29 "${P}-s1l1" >/dev/null
    docker network create --subnet 10.0.2.0/29 "${P}-s1l2" >/dev/null
    docker network create --subnet 172.31.101.0/24 "${P}-lan1" >/dev/null
    docker network create --subnet 172.31.102.0/24 "${P}-lan2" >/dev/null

    # Fixtures (the Tarvos image has python3: no extra image to pull).
    docker run -d --name "${P}-tacacs" -v "${HERE}/../integration:/t:ro" -v "${STATE}:/state" \
        --entrypoint python3 "${image}" /t/tacacs_server.py --key "${TACACS_KEY}" \
        --users-file /state/tacacs-users >/dev/null
    docker run -d --name "${P}-syslog" -v "${HERE}:/lab:ro" --entrypoint python3 "${image}" \
        /lab/syslog_collector.py >/dev/null
    TACACS_IP="$(ip_of "${P}-tacacs")"
    SYSLOG_IP="$(ip_of "${P}-syslog")"

    for node in "${NODES[@]}"; do
        node_run "${node}" "${image}"
    done
    for node in "${NODES[@]}"; do
        wait_ready "${P}-${node}"
        link "${node}"
    done
    for n in 1 2; do
        docker run -d --name "${P}-h${n}" --network "${P}-lan${n}" --cap-add NET_ADMIN \
            --entrypoint sleep "${image}" infinity >/dev/null
        docker exec "${P}-h${n}" sh -c "ip addr add 10.1.100.1${n}/24 dev eth0 &&
            ip route add 10.10.0.0/16 via 10.1.100.1"
    done
    {
        echo "LAB_PREFIX=${P}"
        echo "LAB_IMAGE=${image}"
        echo "TACACS_IP=${TACACS_IP}"
        echo "TACACS_KEY=${TACACS_KEY}"
        echo "SYSLOG_IP=${SYSLOG_IP}"
        for node in "${NODES[@]}"; do
            echo "$(echo "${node}" | tr '[:lower:]' '[:upper:]')_IP=$(ip_of "${P}-${node}")"
        done
    } >"${STATE}/lab.env"
    log "up: $(tr '\n' ' ' <"${STATE}/lab.env")"
}

configure() {
    # shellcheck disable=SC1091
    . "${STATE}/lab.env"
    python3 "${HERE}/fabric.py" set "${SPINE_IP}" spine
    python3 "${HERE}/fabric.py" set "${LEAF1_IP}" leaf1
    python3 "${HERE}/fabric.py" set "${LEAF2_IP}" leaf2
}

down() {
    local names=()
    for c in "${NODES[@]}" "${HOSTS[@]}" tacacs syslog; do names+=("${P}-${c}"); done
    docker rm -f "${names[@]}" >/dev/null 2>&1 || true
    for n in "${NETS[@]}"; do docker network rm "${n}" >/dev/null 2>&1 || true; done
    rm -f "${STATE}/lab.env"
}

status() {
    docker ps -a --filter "name=^${P}-" --format '{{.Names}}\t{{.Status}}'
    [ -f "${STATE}/lab.env" ] && cat "${STATE}/lab.env"
}

relink() {
    case "$1" in
        leaf1 | leaf2)
            docker exec "${P}-$1" sh -c 'ip link show red1 >/dev/null 2>&1 ||
                { ip link add red1 type dummy && ip link set red1 up; }'
            ;;
    esac
}

case "${1:-}" in
    up) up "${2:-}" ;;
    relink) relink "${2:?node}" ;;
    configure) configure ;;
    down) down ;;
    status) status ;;
    *)
        echo "usage: $0 up [IMAGE] | configure | relink NODE | down | status" >&2
        exit 2
        ;;
esac
