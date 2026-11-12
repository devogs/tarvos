#!/usr/bin/env bash
# Container smoke test: SSH login, NETCONF <hello> with tarvos-* modules, FRR health.
# Usage: tests/integration/smoke.sh <image>
# Writes metrics.env (BOOT_SECONDS, MEM_USAGE) in the current directory.
set -euo pipefail

IMAGE="${1:?usage: smoke.sh <image>}"
NAME="tarvos-smoke-$$"
SSH_HOST_PORT="${SSH_HOST_PORT:-2222}"
NETCONF_HOST_PORT="${NETCONF_HOST_PORT:-8300}"
YANG_SRC="$(cd "$(dirname "$0")/../.." && pwd)/yang"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

cleanup() {
    docker logs "${NAME}" >&2 2>&1 || true
    docker rm -f "${NAME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

start=$(date +%s)
docker run -d --name "${NAME}" \
    --cap-add NET_ADMIN --cap-add NET_RAW \
    --sysctl net.ipv4.tcp_l3mdev_accept=1 --sysctl net.ipv4.udp_l3mdev_accept=1 \
    -p "${SSH_HOST_PORT}:22" -p "${NETCONF_HOST_PORT}:830" \
    -v "${YANG_SRC}:/opt/tarvos/yangs:ro" \
    "${IMAGE}" >/dev/null

# Wait for NETCONF (the last management daemon to come up).
ready=0
for _ in $(seq 1 90); do
    if sshpass -p admin ssh "${SSH_OPTS[@]}" -p "${NETCONF_HOST_PORT}" -s admin@127.0.0.1 netconf \
        </dev/null >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done
[ "${ready}" -eq 1 ] || fail "NETCONF did not become ready"
boot_seconds=$(($(date +%s) - start))

echo "== SSH login with admin/admin"
out="$(sshpass -p admin ssh "${SSH_OPTS[@]}" -p "${SSH_HOST_PORT}" admin@127.0.0.1 'echo tarvos-ok')"
[ "${out}" = "tarvos-ok" ] || fail "SSH login returned: ${out}"

echo "== NETCONF <hello> advertises tarvos modules"
hello="$(printf '%s\n]]>]]>\n' \
    '<hello xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><capabilities><capability>urn:ietf:params:netconf:base:1.0</capability></capabilities></hello>' |
    timeout 10 sshpass -p admin ssh "${SSH_OPTS[@]}" -p "${NETCONF_HOST_PORT}" -s admin@127.0.0.1 netconf 2>/dev/null || true)"
for module in tarvos-nftables tarvos-qos tarvos-aaa; do
    grep -q "urn:tarvos:params:xml:ns:yang:${module}" <<<"${hello}" || fail "${module} not advertised"
done

echo "== eth0 in VRF mgmt (informational: depends on host kernel VRF support)"
if docker exec "${NAME}" ip -o link show eth0 | grep -q 'master mgmt '; then
    echo "eth0 is in VRF mgmt"
else
    echo "WARNING: eth0 is not in VRF mgmt on this host" >&2
fi

echo "== FRR health"
docker exec "${NAME}" vtysh -c 'show version' | grep -q 'FRRouting' || fail "vtysh failed"
docker exec "${NAME}" pgrep -x zebra >/dev/null || fail "zebra not running"
docker exec "${NAME}" pgrep -x bgpd >/dev/null || fail "bgpd not running"

mem="$(docker stats --no-stream --format '{{.MemUsage}}' "${NAME}" | awk '{print $1}')"
printf 'BOOT_SECONDS=%s\nMEM_USAGE=%s\n' "${boot_seconds}" "${mem}" >metrics.env
echo "PASS (boot ${boot_seconds}s, mem ${mem})"
