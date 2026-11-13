#!/usr/bin/env bash
# Select which FRR daemons run, from FRR_DAEMONS (space/comma separated, optional [ ] and
# quotes: "zebra bgpd", "[zebra, ospfd]"). FRR 10.7 always starts mgmtd, zebra and staticd
# (frrinit.sh ignores their setting); unset/empty means only those.
# Rewrites ${FRR_DAEMONS_FILE} (the file read by frrinit.sh) and never fails the boot.
set -uo pipefail

FRR_DAEMONS_FILE="${FRR_DAEMONS_FILE:-/etc/frr/daemons}"
KNOWN=(zebra bgpd ospfd ospf6d ripd ripngd isisd pimd pim6d ldpd nhrpd eigrpd babeld sharpd
       pbrd bfdd fabricd vrrpd pathd staticd)
log() { echo "[frr] $*" >&2; }

raw="${FRR_DAEMONS:-}"
raw="${raw//[\[\]\"\',]/ }"
raw="${raw,,}"

declare -A want=([zebra]=1 [staticd]=1)
for d in ${raw}; do
    case "${d}" in
        lldpd) log "lldpd is not an FRR daemon (use LLDP_ENABLED): ignored" ;;
        mgmtd) ;; # always started by FRR
        *)
            known=0
            for k in "${KNOWN[@]}"; do [ "${k}" = "${d}" ] && known=1; done
            if [ "${known}" -eq 1 ]; then
                want["${d}"]=1
            else
                log "unknown FRR daemon '${d}': ignored"
            fi
            ;;
    esac
done

[ -f "${FRR_DAEMONS_FILE}" ] || { log "WARNING: ${FRR_DAEMONS_FILE} not found"; exit 0; }

enabled=()
for k in "${KNOWN[@]}"; do
    if [ -n "${want[${k}]:-}" ]; then
        value=yes
        enabled+=("${k}")
    else
        value=no
    fi
    if grep -qE "^${k}=" "${FRR_DAEMONS_FILE}"; then
        sed -i -E "s/^${k}=.*/${k}=${value}/" "${FRR_DAEMONS_FILE}"
    else
        echo "${k}=${value}" >>"${FRR_DAEMONS_FILE}"
    fi
done
log "enabled: mgmtd ${enabled[*]}"
