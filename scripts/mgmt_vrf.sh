#!/usr/bin/env bash
# Place the management interface (default eth0) into a dedicated VRF (default "mgmt").
# Runs first at boot so sshd/netopeer2 start with the final network layout.
# Idempotent; on any failure it warns and leaves the interface untouched so that
# management access is never lost.
set -uo pipefail

ENABLED="${MGMT_VRF_ENABLED:-yes}"
VRF="${MGMT_VRF_NAME:-mgmt}"
TABLE="${MGMT_VRF_TABLE:-1001}"
IFACE="${MGMT_INTERFACE:-eth0}"

log() { echo "[mgmt-vrf] $*" >&2; }

[ "${ENABLED}" = "yes" ] || { log "disabled (MGMT_VRF_ENABLED=${ENABLED})"; exit 0; }

# Validate inputs: they end up as arguments to ip(8).
[[ "${VRF}" =~ ^[a-z][a-z0-9_-]{0,14}$ ]] || { log "ERROR: invalid MGMT_VRF_NAME"; exit 1; }
[[ "${IFACE}" =~ ^[A-Za-z0-9_.@-]{1,15}$ ]] || { log "ERROR: invalid MGMT_INTERFACE"; exit 1; }
if ! [[ "${TABLE}" =~ ^[0-9]{1,9}$ ]] || [ "${TABLE}" -lt 1 ] || { [ "${TABLE}" -ge 253 ] && [ "${TABLE}" -le 255 ]; }; then
    log "ERROR: invalid MGMT_VRF_TABLE (1-252 or 256+)"
    exit 1
fi

ip link show "${IFACE}" >/dev/null 2>&1 || { log "WARNING: ${IFACE} not found, skipping"; exit 0; }

if [ "$(ip -o link show "${IFACE}" | grep -c "master ${VRF} ")" -gt 0 ]; then
    log "${IFACE} already in VRF ${VRF}"
    exit 0
fi

# Enslaving moves the connected routes into the VRF table and drops the default
# route from the main table, so remember the gateways and re-add them afterwards.
gw4="$(ip -4 route show default dev "${IFACE}" | awk '/default/ {print $3; exit}')"
gw6="$(ip -6 route show default dev "${IFACE}" | awk '/default/ {print $3; exit}')"

if ! ip link show "${VRF}" >/dev/null 2>&1; then
    ip link add "${VRF}" type vrf table "${TABLE}" || { log "WARNING: VRF unsupported by kernel, ${IFACE} left in default VRF"; exit 0; }
fi
ip link set "${VRF}" up || { log "WARNING: cannot bring up ${VRF}"; exit 0; }

if ! ip link set "${IFACE}" master "${VRF}"; then
    log "WARNING: cannot enslave ${IFACE} to ${VRF}"
    exit 0
fi

[ -z "${gw4}" ] || ip -4 route replace default via "${gw4}" dev "${IFACE}" table "${TABLE}" || log "WARNING: IPv4 default route not restored"
[ -z "${gw6}" ] || ip -6 route replace default via "${gw6}" dev "${IFACE}" table "${TABLE}" || log "WARNING: IPv6 default route not restored"

# Outbound connections (syslog, TACACS+) from sockets not bound to the VRF only consult
# the main table, which no longer holds the management routes: fall through to them.
# The rule comes after main (32766) and default (32767): the management table has a
# default route, so placing it earlier would send all routed traffic out of ${IFACE}.
ip -4 rule show | grep -q "lookup ${TABLE}" || ip -4 rule add pref 32800 lookup "${TABLE}" || log "WARNING: IPv4 fallback rule not added"
ip -6 rule show | grep -q "lookup ${TABLE}" || ip -6 rule add pref 32800 lookup "${TABLE}" 2>/dev/null || true

# Let daemons that are not VRF-aware (sshd, netopeer2) accept connections arriving in the VRF.
for key in net.ipv4.tcp_l3mdev_accept net.ipv4.udp_l3mdev_accept; do
    sysctl -q -w "${key}=1" 2>/dev/null || log "WARNING: could not set ${key} (set it via container sysctls)"
done

log "${IFACE} is now in VRF ${VRF} (table ${TABLE})"
