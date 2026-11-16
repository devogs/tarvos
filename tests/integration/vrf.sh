#!/usr/bin/env bash
# VRF and interface entries created and removed over NETCONF only, checked over NETCONF
# and in FRR (vtysh):
#   1. entries with no kernel device (frr-vrf, frr-interface) are deleted, not left behind
#      as "inactive (configured)" stubs;
#   2. a kernel VRF (tarvos-vrf) with a member port, its frr-vrf/frr-interface entries
#      removed first and the VRF last, leaves nothing in FRR;
#   3. the management interface is refused as a VRF member and stays in VRF mgmt.
# Needs the host kernel's vrf module. Usage: tests/integration/vrf.sh <image>
set -euo pipefail

IMAGE="${1:?usage: vrf.sh <image>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
NODE="vrf-$$"
FRR="http://frrouting.org/yang/"
TVRF="urn:tarvos:params:xml:ns:yang:tarvos-vrf"
NC='xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0"'

cleanup() {
    status=$?
    [ "${status}" -eq 0 ] || docker logs "${NODE}" 2>&1 | grep -E 'netdev-sync|frr-sync|Traceback' | tail -20 >&2 || true
    docker rm -f "${NODE}" >/dev/null 2>&1 || true
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

docker run -d --name "${NODE}" --hostname vrf-node \
    --cap-add NET_ADMIN --cap-add NET_RAW --cap-add SYS_ADMIN --cap-add BPF --cap-add PERFMON \
    --sysctl net.ipv4.tcp_l3mdev_accept=1 --sysctl net.ipv4.udp_l3mdev_accept=1 \
    -e "FRR_DAEMONS=[zebra, bgpd]" -e TELEMETRY_ENABLED=no "${IMAGE}" >/dev/null
IP="$(docker inspect -f '{{.NetworkSettings.IPAddress}}' "${NODE}")"

edit() { python3 "${HERE}/netconf_edit.py" "${IP}" 830; }
vtysh() { docker exec "${NODE}" vtysh "$@"; }
# FRR's own view: VRFs and interfaces, running configuration.
frr_view() { vtysh -c 'show vrf' -c 'show running-config'; }
config_names() {  # names in the running datastore of a module (subtree filter)
    python3 - "${IP}" "$1" <<'PY'
import sys
from ncclient import manager
with manager.connect(host=sys.argv[1], port=830, username="admin", password="admin",
                     hostkey_verify=False, look_for_keys=False, allow_agent=False, timeout=60) as m:
    print(m.get_config(source="running", filter=("subtree", sys.argv[2])).data_xml)
PY
}

for _ in $(seq 1 60); do
    python3 "${HERE}/netconf_caps.py" "${IP}" 830 admin admin tarvos-vrf frr-vrf frr-interface >/dev/null 2>&1 && break
    sleep 1
done
python3 "${HERE}/netconf_caps.py" "${IP}" 830 admin admin tarvos-vrf frr-vrf frr-interface >/dev/null ||
    fail "NETCONF did not become ready"
for _ in $(seq 1 30); do vtysh -c 'show vrf' >/dev/null 2>&1 && break; sleep 1; done

echo "== entries without a kernel device: created and removed over NETCONF"
edit <<XML || fail "cannot create TESTVRF/ghost0"
<lib xmlns="${FRR}vrf"><vrf><name>TESTVRF</name></vrf></lib>
<lib xmlns="${FRR}interface"><interface><name>ghost0</name><description>test</description></interface></lib>
XML
view="$(frr_view)"
grep -q 'vrf TESTVRF inactive' <<<"${view}" || fail "TESTVRF not in FRR after creation: ${view}"
grep -q '^interface ghost0' <<<"${view}" || fail "ghost0 not in FRR after creation"
grep -q TESTVRF <<<"$(config_names "<lib xmlns=\"${FRR}vrf\"/>")" || fail "TESTVRF not in the running datastore"
edit <<XML || fail "cannot remove TESTVRF/ghost0"
<lib xmlns="${FRR}vrf"><vrf ${NC} nc:operation="remove"><name>TESTVRF</name></vrf></lib>
<lib xmlns="${FRR}interface"><interface ${NC} nc:operation="remove"><name>ghost0</name></interface></lib>
XML
view="$(frr_view)"
grep -q 'TESTVRF' <<<"${view}" && fail "TESTVRF left in FRR after removal (stub): ${view}"
grep -q 'ghost0' <<<"${view}" && fail "ghost0 left in FRR after removal"
grep -q TESTVRF <<<"$(config_names "<lib xmlns=\"${FRR}vrf\"/>")" && fail "TESTVRF still in the running datastore"

echo "== kernel VRF: remove the FRR entries first, then the tarvos-vrf entry"
if ! docker exec "${NODE}" ip link add vt0 type veth peer name vt0p 2>/dev/null; then
    echo "WARNING: cannot create a veth pair on this host, skipping the kernel VRF checks" >&2
else
    docker exec "${NODE}" ip link set vt0 up
    edit <<XML || fail "cannot create VRF RED"
<vrfs xmlns="${TVRF}"><vrf><name>RED</name><table>1010</table><interface>vt0</interface></vrf></vrfs>
<lib xmlns="${FRR}vrf"><vrf><name>RED</name><zebra xmlns="${FRR}zebra"><l3vni-id>5000</l3vni-id></zebra></vrf></lib>
<lib xmlns="${FRR}interface"><interface><name>vt0</name><description>d</description></interface></lib>
XML
    for _ in $(seq 1 10); do
        view="$(frr_view)"
        grep -q 'vrf RED id' <<<"${view}" && break
        sleep 1
    done
    grep -q 'vrf RED id' <<<"${view}" || fail "VRF RED not active in FRR: ${view}"
    edit <<XML || fail "cannot remove the frr-vrf/frr-interface entries"
<lib xmlns="${FRR}vrf"><vrf ${NC} nc:operation="remove"><name>RED</name></vrf></lib>
<lib xmlns="${FRR}interface"><interface ${NC} nc:operation="remove"><name>vt0</name></interface></lib>
XML
    edit <<XML || fail "cannot remove VRF RED"
<vrfs xmlns="${TVRF}"><vrf ${NC} nc:operation="remove"><name>RED</name></vrf></vrfs>
XML
    for _ in $(seq 1 15); do
        view="$(frr_view)"
        grep -q 'vrf RED' <<<"${view}" || break
        sleep 1
    done
    grep -q 'vrf RED' <<<"${view}" && fail "VRF RED left in FRR after removing the kernel VRF: ${view}"
    docker exec "${NODE}" ip -o link show RED >/dev/null 2>&1 && fail "kernel VRF RED still exists"
    docker exec "${NODE}" ip link del vt0 2>/dev/null || true
fi

echo "== the management interface cannot join another VRF"
if grep -q 'master mgmt ' <<<"$(docker exec "${NODE}" ip -o link show eth0)"; then
    msg="$(edit <<XML || true
<vrfs xmlns="${TVRF}"><vrf><name>A</name><table>1020</table><interface>eth0</interface></vrf></vrfs>
XML
    )"
    grep -qi 'management interface' <<<"${msg}" || fail "eth0 as a VRF member was not refused: ${msg}"
    grep -q 'master mgmt ' <<<"$(docker exec "${NODE}" ip -o link show eth0)" || fail "eth0 left VRF mgmt"
    grep -q '<name>A</name>' <<<"$(config_names "<vrfs xmlns=\"${TVRF}\"/>")" && fail "refused VRF A is in the running datastore"
else
    echo "WARNING: eth0 is not in VRF mgmt on this host, skipping" >&2
fi
echo "vrf: all checks passed"
